"""Real-arm skills: the executor's steps on the reBot through robotics/arm_server.py, built on the team's motions.

    pick(vial_A)          replay paths/vial_A.py (teleop recording: rest -> vial A, the gripper closes on it)
    carry(well_2)         lift -> flat carry -> lower onto the pour spot over well_2 (episode.plan_a_to_b, the same
                          moves as `grab.py transfer`); the spot is layout.yaml pour_spots.well_2 (taught in
                          spots.yaml). Without a pour spot, a measured route (layout.yaml routes) is used instead.
    pour(well_2, 1 mL)    camera pour on the wrist roll: the level model on the calibrated boxes, slows as it fills,
                          tilts back upright on target / spill / overfill / camera lost
    verify(well_2, 1)     what the camera says the compartment GAINED since the pour began
    carry(rack)           the carry moves played backwards: back over the vial's slot
    place(vial_A)         the recorded grab backwards (the gripper releases, the arm goes back to rest)
    lift(mm) / lower(mm)  straight up / down, the vial kept as upright as it is

A skill with nothing recorded or taught for it is a placeholder (when allowed): it prints what it would do and
the arm does nothing for that step. Every move is planned (and refused) before it starts. Skills never raise; on
any failure the executor calls safe_stop(): the wrist is upright (the pour always tilts back) and the arm HOLDS
where it is, vial in hand.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

import numpy as np

from . import telemetry
from .skills import Pose, SkillResult, Skills, logged
from .telemetry import REPO_ROOT

ROBOTICS = REPO_ROOT / "robotics"
for _p in (ROBOTICS, REPO_ROOT / "sense"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

EMPTY_GRIP_DEG = 12.0       # arm_server: jaws closed past this = nothing in them
SETTLE_S = 0.8              # verify: let the surface settle before reading it
MOVE_SPEED_DEG_S = 15.0     # joint-space move to the start of a recorded motion


@dataclass
class RealWorld:
    """What the skills believe about the bench, from layout.yaml and their own results."""
    vials: dict[str, dict]                     # vial id -> {"liquid", "ml", "slot"}
    targets: dict[str, dict]                   # compartment -> {"column", "capacity_ml"}
    held: str | None = None
    at: str | None = None                      # slot or compartment the gripper is at (None = unknown / moved freely)
    before_ml: dict[str, float] = field(default_factory=dict)   # compartment -> mL when its pour began
    out_moves: list = field(default_factory=list)                # the carry out, to play backwards for the return

    @property
    def rack(self) -> dict[str, str | None]:
        """slot -> vial id (None = empty), the shape bench_view() expects."""
        return {v["slot"]: (vid if self.held != vid else None) for vid, v in self.vials.items()}


def slot_of(vial_id: str) -> str:
    """vial_A sits in slot_A."""
    return "slot_" + vial_id.split("_", 1)[-1]


def world_from_layout(layout: dict) -> RealWorld:
    import transfer as T
    vial_ml = float(layout.get("vial_ml", 8.0))
    vials = {vid: {"liquid": liq, "ml": vial_ml, "slot": slot_of(vid)} for vid, liq in (layout.get("vials") or {}).items()}
    for vid in layout.get("routes") or {}:
        vials.setdefault(vid, {"liquid": None, "ml": vial_ml, "slot": slot_of(vid)})
    cfg = layout.get("pour") or {}
    cap = T.capacity_ml(layout)
    targets = {w: {"column": T.camera_column(cfg, w), "capacity_ml": cap} for w in (cfg.get("columns") or {})}
    return RealWorld(vials, targets)


class RealArm:
    """The few arm calls the executor makes itself (grasp check, e-stop state); motion is in the skills."""

    stopped = False

    def __init__(self, client):
        self.client = client

    def gripper_stalled(self) -> bool:
        if getattr(self.client, "dry_run", False):
            return True
        st = self.client.status() or {}
        pos = (st.get("gripper") or {}).get("pos")
        return pos is not None and pos > EMPTY_GRIP_DEG

    def move_q(self, q, duration_s):            # the executor uses skills.safe_stop() on the real arm instead
        from .arm import ArmError
        raise ArmError("the real arm does not jump to joint poses; it holds where it is")


def load_motions() -> dict[str, dict]:
    """robotics/paths/<name>.py (recorded teleop motions), by name. Empty if none."""
    try:
        from paths import load, names
        return {n: load(n) for n in names()}
    except Exception:
        return {}


class RealSkills(Skills):
    PLACEHOLDER = {
        "pick": "placeholder: no recorded grab for this vial yet (robotics/paths), so it must already be in the gripper",
        "place": "placeholder: no recorded grab for this vial yet, so the arm keeps holding it",
    }

    def __init__(self, client=None, layout: dict | None = None, spots: dict | None = None, cam: str = "0",
                 boxes: str | None = None, camera_factory=None, view: bool = False,
                 placeholders: tuple[str, ...] = (), stop_requested=None, motions: dict | None = None):
        import motion as M
        import transfer as T
        from arm_client import ArmClient
        from spots import load_spots
        self.M, self.T = M, T
        self.client = client or ArmClient()
        self.layout = layout if layout is not None else T.load_layout()
        self.spots = spots if spots is not None else load_spots()
        self.motions = motions if motions is not None else load_motions()
        self.cfg = self.layout.get("pour") or {}
        self.world = world_from_layout(self.layout)
        self.cam, self.boxes, self.view = cam, boxes, view
        self._camera_factory = camera_factory or (lambda column: T.CameraPour(
            self.cam, column, T.capacity_ml(self.layout), boxes=self.boxes))
        self._camera = None
        self.max_tilt = float(self.cfg.get("max_tilt_deg", 85))
        self.placeholders = set(placeholders)          # skills allowed to stand in when nothing is recorded
        self.stop_requested = stop_requested or (lambda: False)   # STOP button: ends a pour at once

    # ------------------------------------------------------------ what is set up

    def spot(self, name: str | None):
        return self.spots.get(name) if name else None

    def pour_spot(self, target: str) -> str | None:
        return (self.layout.get("pour_spots") or {}).get(target)

    def has_motion(self, vial_id: str) -> bool:
        return vial_id in self.motions

    def can_pick(self, vial_id: str) -> bool:
        return self.has_motion(vial_id) or "pick" in self.placeholders

    def reachable(self, vial_id: str, target: str) -> str:
        """'' if the arm can carry this vial to the target, else why not."""
        if self.spot(self.pour_spot(target)) is not None:
            return ""
        try:
            self.T.route(self.layout, vial_id, target)
        except (KeyError, ValueError):
            spot = self.pour_spot(target)
            return (f"no pour spot for {target}: set layout.yaml pour_spots.{target} to a taught spot"
                    if not spot else f"pour spot {spot} for {target} is not taught in spots.yaml")
        return ""

    def not_ready(self) -> list[str]:
        """What the bench still needs before real jobs can run (empty = ready)."""
        out = []
        unrecorded = [v for v in self.world.vials if not self.has_motion(v)]
        if unrecorded and "pick" not in self.placeholders:
            out.append(f"no recorded grab for {', '.join(unrecorded)} (python grab.py save ...)")
        unreachable = [t for t in self.world.targets if any(self.reachable(v, t) for v in self.world.vials)]
        if unreachable:
            out.append(f"no pour spot taught for {', '.join(unreachable)} (layout.yaml pour_spots + spots.yaml)")
        return out

    def bench_view(self) -> dict:
        w = self.world
        return {"real": True, "station_target": None, "station_tube": None, "tube_capacity_ml": None,
                "slots": sorted(v["slot"] for v in w.vials.values()),
                "vials": {vid: {"slot": v["slot"], "liquid": v["liquid"], "ml": round(v["ml"], 2),
                                "taught": self.can_pick(vid)}
                          for vid, v in w.vials.items() if w.held != vid},
                "targets": {t: dict(d) for t, d in w.targets.items()},
                "routes": {vid: [t for t in w.targets if not self.reachable(vid, t)] for vid in w.vials},
                "not_ready": self.not_ready()}

    # ------------------------------------------------------------ helpers

    def _q(self) -> dict[int, float]:
        return self.client.current_joints()

    def _run(self, paths) -> None:
        for p in paths:
            self.client.run(p)

    def _play(self, m: dict, reverse: bool = False) -> None:
        """Replay a recorded motion through arm_server, as grab.py play does: move to its start, set the
        gripper, then the whole path continuously."""
        pts = m["points"][::-1] if reverse else m["points"]
        first = pts[0]
        self.client._send({"op": "move", "joints": {str(j): first[j] for j in range(1, 7)}, "speed": MOVE_SPEED_DEG_S})
        self.client._send({"op": "gripper", "target": first[7], "speed": 30.0})
        self.client._send({"op": "path", "points": [{str(j): round(p[j], 3) for j in range(1, 8)} for p in pts],
                           "hz": m["hz"]})

    def camera(self, column: str):
        """The side camera, opened once (and checked) on first use and kept for the whole job."""
        if self._camera is None:
            self._camera = self._camera_factory(column)
        self._camera.column = column
        return self._camera

    def close(self) -> None:
        if self._camera is not None:
            self._camera.close()
            self._camera = None

    def safe_stop(self) -> tuple[bool, str]:
        """After a failed step: the pour has already tilted the wrist back upright; hold right here."""
        held = f" with {self.world.held} in the gripper" if self.world.held else ""
        return True, f"wrist upright, arm holding where it is{held}; a person decides the next move"

    # ------------------------------------------------------------ skills

    def locate(self, vial_id):
        m = self.motions.get(vial_id)
        if not m:
            return None
        p, R = self.M.fk_mm({j: m["initial_position"][j] for j in range(1, 7)})
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = R, np.asarray(p) / 1000.0
        return Pose(slot_of(vial_id), T)

    @logged
    def pick(self, vial_id):
        w = self.world
        if w.held:
            return SkillResult(False, f"already holding {w.held}")
        if vial_id not in w.vials:
            return SkillResult(False, f"unknown vial {vial_id}; layout.yaml has {', '.join(w.vials)}")
        slot = w.vials[vial_id]["slot"]
        m = self.motions.get(vial_id)
        if m is None:
            if "pick" not in self.placeholders:
                return SkillResult(False, f"no recorded grab for {vial_id}: python grab.py save {vial_id} --rec ...")
            w.held, w.at, w.out_moves = vial_id, slot, []
            return SkillResult(True, "", {"slot": slot, "placeholder": True, "note": self.PLACEHOLDER["pick"],
                                          "would": f"pick up {vial_id} from {slot}"})
        self._play(m)
        w.held, w.at, w.out_moves = vial_id, slot, []
        return SkillResult(True, "", {"slot": slot, "motion": vial_id, "points": len(m["points"])})

    @logged
    def carry(self, to):
        w, M, T = self.world, self.M, self.T
        if not w.held:
            return SkillResult(False, "nothing in the gripper to carry")
        home = w.vials[w.held]["slot"]
        try:
            if to in w.targets:                                   # out: the vial's slot -> over the compartment
                if w.at != home:
                    return SkillResult(False, f"carry to {to} starts from {home}, but the gripper is at {w.at}")
                spot = self.spot(self.pour_spot(to))
                if spot is not None:
                    from episode import plan_a_to_b
                    q0 = self._q()
                    paths = plan_a_to_b({"A": {"command": {str(j): v for j, v in q0.items()}},
                                         "B": {"command": {str(j): v for j, v in spot.items()}}})
                else:
                    lift, lower = float(self.layout.get("lift_mm", 150)), float(self.layout.get("lower_mm", 110))
                    paths = M.plan_transfer(self._q(), T.route(self.layout, w.held, to), lift_mm=lift, lower_mm=lower)
                room = T.roll_room_deg(paths[-1].final_joints[6], T.pour_direction(self.cfg))
                if room < 20:
                    return SkillResult(False, f"only {room:.0f} deg of wrist roll left to pour at {to}: nothing moved")
                self._run(paths)
                w.at, w.out_moves = to, paths
            elif to in ("rack", home):                            # back: the same moves played backwards
                if w.at == home:
                    return SkillResult(True, "", {"note": f"already at {home}"})
                if w.at not in w.targets or not w.out_moves:
                    return SkillResult(False, f"don't know the way back from {w.at}")
                from episode import reverse_path
                paths = [reverse_path(p) for p in reversed(w.out_moves)]
                self._run(paths)
                w.at, w.out_moves = home, []
            else:
                return SkillResult(False, f"unknown carry target '{to}': a compartment ({', '.join(w.targets)}) or rack")
        except (M.PlanError, KeyError, ValueError) as e:
            return SkillResult(False, f"cannot plan the carry, nothing moved: {e}")
        return SkillResult(True, "", {"at": w.at, "moves_s": round(sum(p.duration_s for p in paths), 1)})

    @logged
    def lift(self, mm):
        return self._straight(float(mm), up=True)

    @logged
    def lower(self, mm):
        return self._straight(float(mm), up=False)

    def _straight(self, mm: float, up: bool) -> SkillResult:
        if not 1 <= mm <= 150:
            return SkillResult(False, f"{mm:g} mm: move between 1 and 150 mm")
        try:
            p = (self.M.plan_lift if up else self.M.plan_lower)(self._q(), mm)
        except self.M.PlanError as e:
            return SkillResult(False, f"cannot plan that, nothing moved: {e}")
        self._run([p])
        self.world.at, self.world.out_moves = None, []     # moved freely: a carry has to start from a known place
        return SkillResult(True, "", {"moved_mm": mm if up else -mm})

    @logged
    def pour(self, target, ml, liquid):
        w = self.world
        if target not in w.targets:
            return SkillResult(False, f"unknown compartment '{target}'")
        if not w.held or w.at != target:
            return SkillResult(False, f"must hold a vial above {target} before pouring (at {w.at})")
        vial = w.vials[w.held]
        if vial["liquid"] and liquid and vial["liquid"] != liquid:
            return SkillResult(False, f"held vial {w.held} holds {vial['liquid']}, not {liquid}")
        column = w.targets[target]["column"]
        try:
            cam = self.camera(column)
            w.before_ml[target] = cam.level()[1]

            def tick(rec):
                if self.stop_requested():
                    raise KeyboardInterrupt                     # pour_motor6: "stopped by operator", tilts back
                if not rec.get("final"):
                    telemetry.log("pour.sample", {**rec, "target": target, "column": column,
                                                  "ml_now": rec.get("ml")})     # the column's level now, mL
            r, added = cam.pour_ml(self.T.pour_direction(self.cfg), float(ml), self.max_tilt, view=self.view,
                                   on_tick=tick)
        except RuntimeError as e:
            return SkillResult(False, f"pour not started: {e}")
        if added is not None:
            vial["ml"] = max(0.0, vial["ml"] - added)
        info = {"dispensed_ml": added, "target_ml": ml, "column": column, "result": r.result,
                "max_tilt_deg": r.max_tilt_deg, "duration_s": r.duration_s}
        if not r.ok:
            return SkillResult(False, f"pour {r.result}: {r.reason}", info)
        if added is None:
            return SkillResult(False, "pour finished but the camera could not read how much went in", info)
        return SkillResult(True, "", info)

    @logged
    def verify(self, target, expect_ml, tol):
        w = self.world
        if target not in w.targets:
            return SkillResult(False, f"unknown compartment '{target}'")
        if target not in w.before_ml:
            return SkillResult(False, f"nothing was poured into {target} in this job")
        time.sleep(SETTLE_S)
        try:
            _, now_ml, _ = self.camera(w.targets[target]["column"]).level(fresh_s=2.0)
        except RuntimeError as e:
            return SkillResult(False, f"cannot verify: {e}")
        measured = now_ml - w.before_ml[target]
        err = measured - expect_ml
        info = {"measured_ml": round(measured, 3), "expect_ml": expect_ml, "error_ml": round(err, 3), "tol_ml": tol,
                "column_ml": round(now_ml, 3)}
        if abs(err) > tol:
            return SkillResult(False, f"{target} gained {measured:.2f} mL, expected {expect_ml} ± {tol} mL", info)
        return SkillResult(True, "", info)

    @logged
    def place(self, vial_id, slot):
        w = self.world
        if w.held != vial_id:
            return SkillResult(False, f"not holding {vial_id} (holding {w.held})")
        if slot != w.vials[vial_id]["slot"]:
            return SkillResult(False, f"{vial_id} goes back to {w.vials[vial_id]['slot']}, not {slot}")
        if w.at != slot:
            return SkillResult(False, f"the gripper is at {w.at}, not above {slot}: carry it to the rack first")
        m = self.motions.get(vial_id)
        if m is None:
            if "place" not in self.placeholders:
                return SkillResult(False, f"no recorded grab for {vial_id} to play backwards")
            w.held = None
            return SkillResult(True, "", {"slot": slot, "placeholder": True, "note": self.PLACEHOLDER["place"],
                                          "would": f"put {vial_id} back in {slot} and let go"})
        self._play(m, reverse=True)
        w.held, w.at = None, None
        return SkillResult(True, "", {"slot": slot, "motion": f"{vial_id} backwards"})
