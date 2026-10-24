"""Skills contract (Robotics + Sense own the real ones) and FakeSkills that drive FakeArm + FakeLevelTracker.

Skills never raise: every outcome, including arm errors and e-stops, comes back as a SkillResult.
Every call logs skill.start and skill.end to telemetry.
"""
from __future__ import annotations

import functools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

from . import telemetry
from .arm import ArmError, ArmStopped, FakeArm
from .bench import pose, rack_slots
from .level import FakeLevelTracker


@dataclass
class SkillResult:
    ok: bool
    reason: str = ""
    telemetry: dict = field(default_factory=dict)


@dataclass
class Pose:
    slot: str
    T: np.ndarray  # 4x4, arm base frame, metres


def logged(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        name = fn.__name__
        telemetry.log("skill.start", {"skill": name, "args": list(args), "kwargs": kwargs})
        t0 = time.monotonic()
        try:
            res = fn(self, *args, **kwargs)
        except ArmStopped as e:
            res = SkillResult(False, f"e-stop: {e}", {"estop": True})
        except ArmError as e:
            res = SkillResult(False, f"arm error: {e}")
        res.telemetry.setdefault("duration_s", round(time.monotonic() - t0, 3))
        telemetry.log("skill.end", {"skill": name, "ok": res.ok, "reason": res.reason, "telemetry": res.telemetry})
        return res
    return wrapper


class Skills(ABC):
    @abstractmethod
    def locate(self, vial_id: str) -> Pose | None: ...

    @abstractmethod
    def pick(self, vial_id: str) -> SkillResult: ...

    @abstractmethod
    def carry(self, to: str) -> SkillResult:
        """to: 'station', 'rack' or 'home'."""

    @abstractmethod
    def pour(self, target: str, ml: float, liquid: str) -> SkillResult:
        """telemetry includes dispensed_ml."""

    @abstractmethod
    def verify(self, target: str, expect_ml: float, tol: float) -> SkillResult:
        """telemetry includes measured_ml and error_ml."""

    @abstractmethod
    def place(self, vial_id: str, slot: str) -> SkillResult: ...


# ---------------------------------------------------------------- fakes

FAULTS = {"wrong_tube", "grasp_slip", "drop_in_carry", "pour_overshoot", "level_stuck"}

# Simulated liquids: flow = k * (tilt - onset) mL/s above onset, lag = s from tilt-back until flow stops.
FAKE_LIQUIDS = {
    "blue": {"onset": 0.95, "k": 4.0, "lag": 0.25, "setpoint": 1.0},  # water + blue dye
    "red": {"onset": 1.10, "k": 1.5, "lag": 0.50, "setpoint": 0.6},   # 50/50 glycerin-water + red dye
}
CARRY_TARGETS = {"station": "station_pre", "rack": "survey", "home": "home"}


@dataclass
class FakeWorld:
    rack: dict[str, str | None]          # slot -> vial id
    vials: dict[str, dict]               # vial id -> {"type", "liquid", "ml"}
    station_tube: str = "tube_13mm"      # tube type actually sitting in the station
    held: str | None = None
    at: str = "home"

    @classmethod
    def default(cls, bench: dict) -> FakeWorld:
        rack = {s: None for s in rack_slots(bench)}
        rack["A"], rack["B"] = "V1", "V2"
        vials = {
            "V1": {"type": "vial_10ml", "liquid": "blue", "ml": 8.0},
            "V2": {"type": "vial_10ml", "liquid": "red", "ml": 8.0},
        }
        return cls(rack=rack, vials=vials, station_tube=bench["station"]["tube_type"])


class FakeSkills(Skills):
    def __init__(self, arm: FakeArm, level: FakeLevelTracker, bench: dict,
                 world: FakeWorld | None = None, faults: set[str] | frozenset = frozenset(), seed: int = 0):
        unknown = set(faults) - FAULTS
        if unknown:
            raise ValueError(f"unknown faults {sorted(unknown)}; choose from {sorted(FAULTS)}")
        self.arm, self.level, self.bench = arm, level, bench
        self.world = world or FakeWorld.default(bench)
        self.faults = set(faults)
        self._rng = np.random.default_rng(seed)
        if "grasp_slip" in self.faults:
            arm.faults.add("grasp_slip")
        if "level_stuck" in self.faults:
            level.stuck = True
        if "wrong_tube" in self.faults:
            self.world.station_tube = next(t for t in bench["tubes"] if t != bench["station"]["tube_type"])

    def _vial_od(self, vial_id: str) -> float:
        return self.bench["vial_types"][self.world.vials[vial_id]["type"]]["od_mm"]

    def locate(self, vial_id):
        for slot, vid in self.world.rack.items():
            if vid == vial_id:
                T = np.eye(4)
                T[:3, 3] = [0.20, -0.10 + 0.025 * "ABCDEF".index(slot), 0.05]
                return Pose(slot, T)
        return None

    @logged
    def pick(self, vial_id):
        if self.world.held:
            return SkillResult(False, f"already holding {self.world.held}")
        loc = self.locate(vial_id)
        if loc is None:
            return SkillResult(False, f"vial {vial_id} not found in rack")
        pre, grasp = pose(self.bench, f"rack_{loc.slot}_pre"), pose(self.bench, f"rack_{loc.slot}_grasp")
        od = self._vial_od(vial_id)
        self.arm.move_q(pre, 1.5)
        self.arm.gripper(od + 10)
        self.arm.move_q(grasp, 1.0)
        self.arm.object_mm = od
        self.arm.gripper(od - 2)
        if not self.arm.gripper_stalled():
            self.arm.object_mm = None
            self.arm.gripper(od + 10)
            self.arm.move_q(pre, 1.0)
            return SkillResult(False, "grasp not detected (gripper did not stall)", {"slot": loc.slot})
        self.world.rack[loc.slot] = None
        self.world.held = vial_id
        self.arm.move_q(pre, 1.0)
        self.world.at = "rack"
        return SkillResult(True, "", {"slot": loc.slot})

    @logged
    def carry(self, to):
        if to not in CARRY_TARGETS:
            return SkillResult(False, f"unknown carry target '{to}'; use {sorted(CARRY_TARGETS)}")
        if to == "station" and not self.world.held:
            return SkillResult(False, "nothing held to carry to the station")
        start, goal = np.array(self.arm.get_q()), pose(self.bench, CARRY_TARGETS[to])
        dt, n = 0.02, 100
        s = np.linspace(0, 1, n)
        s = 3 * s**2 - 2 * s**3  # smoothstep; the real carry uses an S-curve with tilt compensation
        path = [start + (goal - start) * si for si in s]
        half = n // 2
        self.arm.stream_q(path[:half], dt)
        if "drop_in_carry" in self.faults and self.world.held:
            self.arm.object_mm = None
            self.arm.gripper(self.arm.width_mm)  # fingers close on nothing
        self.arm.stream_q(path[half:], dt)
        if self.world.held and not self.arm.gripper_stalled():
            dropped, self.world.held = self.world.held, None
            return SkillResult(False, f"grasp lost during carry, vial {dropped} dropped")
        self.world.at = to
        return SkillResult(True)

    @logged
    def pour(self, target, ml, liquid):
        w, b = self.world, self.bench
        if target != b["station"]["target_id"]:
            return SkillResult(False, f"unknown target '{target}'")
        if w.at != "station" or not w.held:
            return SkillResult(False, "must be holding a vial at the station before pouring")
        vial = w.vials[w.held]
        if vial["liquid"] != liquid:
            return SkillResult(False, f"held vial {w.held} contains {vial['liquid']}, not {liquid}")
        if ml > vial["ml"]:
            return SkillResult(False, f"vial {w.held} has {vial['ml']:.1f} mL, cannot pour {ml} mL")
        if liquid not in FAKE_LIQUIDS:
            return SkillResult(False, f"no pour recipe for liquid '{liquid}'")
        p = FAKE_LIQUIDS[liquid]
        j = b["pour_joint"] - 1
        base = pose(b, "station_pour")
        self.arm.move_q(base, 1.0)
        self.level.zero()

        dt, t, tilt, flow = 1 / 30, 0.0, 0.0, 0.0
        ref_ml, ref_t = 0.0, 0.0
        while True:
            t += dt
            measured = self.level.read().ml
            remaining = ml - measured
            if remaining <= flow * p["lag"]:
                break  # cut-off: what is still in flight lands during tilt-back
            setpoint = p["setpoint"] if remaining > 0.6 else 0.3
            tilt += 0.6 * dt if flow < setpoint else -1.0 * dt
            tilt = max(0.0, tilt)
            flow = p["k"] * max(0.0, tilt - p["onset"])
            self.level.add(flow * dt)
            q = base.copy()
            q[j] += tilt
            self.arm.stream_q([q], dt)
            telemetry.log("pour.sample", {"t": round(t, 3), "tilt": round(tilt, 4), "ml": round(measured, 3), "flow": round(flow, 3)})
            # abort if we expect flow but the level is not rising
            if measured > ref_ml + 0.05 or flow < 0.2:
                ref_ml, ref_t = measured, t
            if t - ref_t > 1.0:
                self.arm.stream_q([base], dt)
                return SkillResult(False, "level not rising while tilted, pour aborted", {"tilt": tilt, "t": t})
            if t > 30:
                self.arm.stream_q([base], dt)
                return SkillResult(False, "pour timed out after 30 s")

        self.level.add(flow * p["lag"] * self._rng.uniform(0.9, 1.1))
        if "pour_overshoot" in self.faults:
            self.level.add(0.6)
        self.arm.stream_q([base], dt)
        dispensed = self.level.read().ml
        vial["ml"] -= dispensed
        return SkillResult(True, "", {"dispensed_ml": round(dispensed, 3), "target_ml": ml, "pour_s": round(t, 2)})

    @logged
    def verify(self, target, expect_ml, tol):
        b = self.bench
        if target != b["station"]["target_id"]:
            return SkillResult(False, f"unknown target '{target}'")
        want, seen = b["station"]["tube_type"], self.world.station_tube
        if seen != want:
            return SkillResult(False, f"wrong tube at station: {b['tubes'][seen]['bore_mm']} mm bore, "
                                      f"expected {b['tubes'][want]['bore_mm']} mm ({want})",
                               {"tube_seen": seen, "tube_expected": want})
        samples = [self.level.read() for _ in range(5)]
        if min(s.conf for s in samples) < 0.5:
            return SkillResult(False, "level reading low confidence")
        measured = float(np.mean([s.ml for s in samples]))
        err = measured - expect_ml
        info = {"measured_ml": round(measured, 3), "expect_ml": expect_ml, "error_ml": round(err, 3), "tol_ml": tol}
        if abs(err) > tol:
            return SkillResult(False, f"volume {measured:.2f} mL, expected {expect_ml} ± {tol} mL", info)
        return SkillResult(True, "", info)

    @logged
    def place(self, vial_id, slot):
        w = self.world
        if w.held != vial_id:
            return SkillResult(False, f"not holding {vial_id} (holding {w.held})")
        if slot not in w.rack:
            return SkillResult(False, f"unknown rack slot '{slot}'")
        if w.rack[slot] is not None:
            return SkillResult(False, f"slot {slot} already holds {w.rack[slot]}")
        pre, grasp = pose(self.bench, f"rack_{slot}_pre"), pose(self.bench, f"rack_{slot}_grasp")
        self.arm.move_q(pre, 1.5)
        self.arm.move_q(grasp, 1.0)
        self.arm.object_mm = None
        self.arm.gripper(self._vial_od(vial_id) + 10)
        w.rack[slot], w.held = vial_id, None
        self.arm.move_q(pre, 1.0)
        w.at = "rack"
        return SkillResult(True, "", {"slot": slot})
