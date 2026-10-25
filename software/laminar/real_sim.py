"""The real-arm skills on a simulated bench: same RealSkills, same motion planning (robotics/motion.py), same
executor and checks, but a stand-in arm_server and side camera. For the dashboard and tests without hardware.

    from laminar.real_sim import make_sim_system
    system = make_sim_system(time_scale=0.2)      # moves and pours play at 5x real speed

The bench is robotics/layout.yaml with example routes filled in (the real ones are measured on the bench).
"""
from __future__ import annotations

import copy
import random
import time
import types

from .hardware import System
from .real_skills import ROBOTICS, RealArm, RealSkills

GRAB_POSE = {1: -31.0, 2: 100.0, 3: 11.0, 4: 88.0, 5: 29.0, 6: -5.0}   # real demo grab pose (tube in stand)
SIM_ROUTES = {"well_1": [40.0, -60.0], "well_2": [40.0, -90.0], "well_3": [40.0, -120.0], "well_4": [40.0, -150.0]}


def sim_layout() -> dict:
    """layout.yaml with every route filled in by an example offset (same for every vial)."""
    import sys
    sys.path.insert(0, str(ROBOTICS))
    import transfer as T
    lay = copy.deepcopy(T.load_layout())
    lay["routes"] = {v: dict(SIM_ROUTES) for v in (lay.get("routes") or {"vial_A": {}})}
    return lay


class SimArmServer:
    """arm_server as ArmClient sees it: a path takes its planned time (x time_scale) and moves the commanded pose."""

    dry_run = False

    def __init__(self, time_scale: float = 1.0, start=GRAB_POSE):
        self.q, self.grip, self.time_scale, self.log = dict(start), 30.0, time_scale, []

    def current_joints(self):
        return dict(self.q)

    def run(self, path, mode="path"):
        self.log.append(path.label)
        time.sleep(path.duration_s * self.time_scale)
        self.q = path.final_joints
        return {"result": "ok"}

    def status(self):
        return {"gripper": {"pos": self.grip}, "command": {f"J{i}": v for i, v in self.q.items()}}

    def _send(self, cmd, wait=True):
        self.log.append(cmd["op"])
        if cmd["op"] == "gripper":
            self.grip = float(cmd["target"])
        elif cmd["op"] == "grasp":
            self.grip = 30.0
            return {"result": "contact at 34.0, holding at 30.0", "contact": True}
        return {"result": "ok"}


class SimSideCamera:
    """The side camera on the compartments. A pour fills its column at ~0.4 mL/s, slowing near the target,
    and lands within a few hundredths of a mL; on_tick gets pour_motor6-shaped records and may raise
    KeyboardInterrupt to stop (as the real pour does)."""

    def __init__(self, capacity_ml: float = 4.5, time_scale: float = 1.0, start_ml: dict | None = None,
                 seed: int | None = None):
        self.capacity_ml, self.time_scale = capacity_ml, time_scale
        self.ml = {c: 0.0 for c in "ABCD"} | (start_ml or {})
        self.column = "A"
        self._rng = random.Random(seed)

    def level(self, fresh_s=1.0, column=None):
        ml = self.ml[column or self.column]
        return ml / self.capacity_ml, ml, self.capacity_ml

    def pour_ml(self, direction, add_ml, max_tilt, view=True, on_tick=None):
        col, ml0 = self.column, self.ml[self.column]
        if (ml0 + add_ml) / self.capacity_ml > 0.97:
            raise RuntimeError(f"channel {col} holds {ml0:.1f} of {self.capacity_ml:g} mL: +{add_ml:g} mL would "
                               "overflow (past 97 % of the rim)")
        goal = ml0 + add_ml + self._rng.uniform(-0.04, 0.04)
        t, tilt, dt, result, reason = 0.0, 0.0, 0.1, "done", "target reached"
        try:
            while self.ml[col] < goal - 1e-3:
                tilt = min(max_tilt, tilt + 15.0 * dt)
                left = goal - self.ml[col]
                rate = 0.0 if tilt < 45 else min(0.4, 0.15 + left)            # mL/s: slows near the target
                self.ml[col] = min(goal, self.ml[col] + rate * dt)
                t += dt
                fill = (self.ml[col] - ml0) / add_ml
                if on_tick:
                    on_tick({"t": round(t, 2), "tilt_deg": round(tilt, 1), "fill": round(fill, 4),
                             "rate": round(rate / add_ml, 4), "allowed": 0.25, "ml": round(self.ml[col], 3),
                             "phase": "full" if fill < 0.6 else "final", "action": "tilt_more" if rate == 0 else "hold",
                             "full": round(self.ml[col] / self.capacity_ml, 4), "flow_ml_s": rate})
                time.sleep(dt * self.time_scale)
        except KeyboardInterrupt:
            result, reason = "aborted", "stopped by operator"
        r = types.SimpleNamespace(ok=result == "done", result=result, reason=reason, max_tilt_deg=round(tilt, 1),
                                  duration_s=round(t, 2), final_ml=round(self.ml[col], 3))
        return r, round(self.ml[col] - ml0, 3)

    def close(self):
        pass


def make_sim_system(time_scale: float = 1.0, placeholders=("pick", "place"), stop_requested=None,
                    start_ml: dict | None = None) -> System:
    """RealSkills on a simulated arm and camera, with the example routes."""
    lay = sim_layout()
    server = SimArmServer(time_scale)
    cam = SimSideCamera(float((lay.get("pour") or {}).get("capacity_ml", 4.5)), time_scale, start_ml)
    skills = RealSkills(client=server, layout={**lay, "pour_spots": {}}, spots={}, motions={},
                        camera_factory=lambda column: cam, placeholders=tuple(placeholders),
                        stop_requested=stop_requested)
    skills.sim_camera = cam
    return System(RealArm(server), None, skills, {})
