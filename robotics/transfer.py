"""Vial -> compartment -> vial round trip with a camera-steered pour, from the pose a grab ends in.

    lift 100 -> shift (dx, dy) in that locked horizontal plane -> lower 60
    -> camera pour on the wrist roll (software/laminar/pour_motor6.py: pour_right / pour_left)
    -> lift 60 -> shift (-dx, -dy) -> lower 100

(dx, dy) comes from layout.yaml (the only bench measurements needed). All six moves are planned, and the
camera checked, before the arm moves; each move is played continuously by arm_server.py ("path" op).
The grab before and the release after are separate (recorded) steps.

The pour fills the compartment's camera column (layout.yaml pour.columns) to --target of full, slowing as
it fills, and tilts back upright when done or on wrong column / overfill / camera lost / no flow. The tube
is carried back in every case, except an error from the arm itself (then it holds where it is).

Run from robotics/ with arm_server.py running and the tube already gripped in its slot. The side camera
needs no calibration: draw one box per compartment first (A, B, C, D left to right, as in pour.columns):
    cd sense && python live_view.py --cam <index> --new-boxes

    python transfer.py vial_A well_2                                  # plan only, print the six moves
    python transfer.py vial_A well_2 --cam 2 --run                    # camera pour to 90 % of full
    python transfer.py vial_A well_2 --cam 2 --target 0.5 --run       # half full
    python transfer.py vial_A well_2 --pour fixed --tilt 80 --hold 3 --run   # no camera: fixed tilt
    python transfer.py vial_A well_2 --pour none --run                # moves only, no pour
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path as FilePath

import numpy as np
import yaml

import motion as M
from arm_client import ArmClient

HERE = FilePath(__file__).resolve().parent
LAYOUT_FILE = HERE / "layout.yaml"
sys.path.insert(0, str(HERE.parent / "software"))  # Om's pour (software/laminar/pour_motor6.py)
sys.path.insert(0, str(HERE.parent / "sense"))     # camera + level tracker it uses
DEFAULT_CAPACITY_ML = 4.5  # mL in a full compartment, unless layout.yaml pour.capacity_ml says otherwise
ROLL_MARGIN_DEG = 2.0  # stay this far inside the wrist-roll limit


def load_layout(path: FilePath = LAYOUT_FILE) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def route(layout: dict, vial: str, compartment: str) -> tuple[float, float]:
    """(dx, dy) in mm from vial to compartment. Clear errors for typos and unmeasured routes."""
    routes = layout.get("routes") or {}
    if vial not in routes:
        raise KeyError(f"unknown vial {vial!r}; layout.yaml has {', '.join(routes)}")
    if compartment not in (routes[vial] or {}):
        raise KeyError(f"unknown compartment {compartment!r} for {vial}; known: {', '.join(routes[vial])}")
    d = routes[vial][compartment]
    if d is None:
        raise ValueError(f"routes.{vial}.{compartment} is not measured yet (null in layout.yaml)")
    return float(d[0]), float(d[1])


def roll_room_deg(base_deg: float, direction: int, joint: int = 6) -> float:
    """How far the pour roll can turn from base_deg in `direction` before its soft limit."""
    lo, hi = M.JOINT_LIMITS_DEG[joint]
    room = (hi - base_deg) if direction > 0 else (base_deg - lo)
    return max(0.0, room - ROLL_MARGIN_DEG)


def pour_direction(cfg: dict, side: str | None = None) -> int:
    """+1 = pour_right (J6 +), -1 = pour_left. --side overrides layout.yaml pour.direction."""
    if side:
        return 1 if side == "right" else -1
    return 1 if int(cfg.get("direction", 1)) > 0 else -1


def capacity_ml(layout: dict) -> float:
    """mL in a full compartment (layout.yaml pour.capacity_ml)."""
    return float((layout.get("pour") or {}).get("capacity_ml", DEFAULT_CAPACITY_ML))


def camera_column(cfg: dict, compartment: str) -> str:
    """The side-camera box (A, B, ... as drawn in live_view.py) that shows this compartment."""
    return str((cfg.get("columns") or {}).get(compartment, compartment))


class CameraPour:
    """Side camera + level model (sense/level_seg.onnx) for pour_motor6, opened and checked BEFORE the arm moves.

    No calibration file: the model reads the boxes drawn in `cd sense && python live_view.py --new-boxes`
    (sense/live_boxes.json, named A, B, C, ... left to right, one per compartment); the columns around them
    are watched for spills. Every pour is recorded to sense/seg_data/frames/pour_<date>_<time>/."""

    def __init__(self, cam: str, column: str, capacity_ml: float, boxes: str | FilePath | None = None,
                 seg_model: str | FilePath | None = None, record: bool = True):
        from laminar import pour_motor6 as P6
        import live_view
        from cameras import LatestFrame, open_camera
        path = FilePath(boxes or live_view.BOXES_FILE)
        if not path.exists():
            raise RuntimeError(f"no boxes in {path.name}: cd sense && python live_view.py --cam {cam} --new-boxes, "
                               "one box per compartment (A, B, C, ... left to right)")
        drawn = json.loads(path.read_text())
        names = [chr(ord("A") + i) for i in range(len(drawn))]
        if column not in names:
            raise RuntimeError(f"no box {column}: {path.name} has {', '.join(names) or 'none'}. Redraw with "
                               "python live_view.py --new-boxes, one box per compartment")
        model = FilePath(seg_model or live_view.DEFAULT_MODEL)
        if not model.exists():
            raise RuntimeError(f"{model.name} missing: train it (cd sense && python train_seg.py) or git pull")
        self.P6, self.column, self.capacity_ml = P6, column, float(capacity_ml)
        self.cam = LatestFrame(open_camera(int(cam) if str(cam).isdigit() else cam)).start()
        self.tracker, self.names, _ = live_view.build(self.cam, drawn, str(model), self.capacity_ml,
                                                      live_view.cam_width(self.cam))
        time.sleep(1.5)
        hz = P6.readings_per_s(self.tracker, column)
        if hz < P6.MIN_READ_HZ:
            self.close()
            raise RuntimeError(f"only {hz} level readings a second (need {P6.MIN_READ_HZ}): the computer is too "
                               "busy (training? other heavy apps?) or the camera is slow")
        self.owns_camera = True
        self.recorder = P6.CamRecorder(self.cam, tracker=self.tracker) if record else None
        self._info: dict = {"column": column, "boxes": {k: list(c.roi) for k, c in self.tracker.columns.items()},
                            "pours": []}

    @classmethod
    def shared(cls, cam, tracker, column: str, capacity_ml: float, record: bool = True) -> "CameraPour":
        """A CameraPour on a camera + level tracker someone else already runs (e.g. the dashboard's bench
        computer, which keeps them open for the live view). close() leaves them running."""
        from laminar import pour_motor6 as P6
        if column not in tracker.columns:
            raise RuntimeError(f"no box {column} in the calibration: calibrate the side camera")
        self = cls.__new__(cls)
        self.P6, self.column, self.capacity_ml = P6, column, float(capacity_ml)
        self.cam, self.tracker, self.names = cam, tracker, list(tracker.columns)
        self.owns_camera = False
        hz = P6.readings_per_s(tracker, column)
        if hz < P6.MIN_READ_HZ:
            raise RuntimeError(f"only {hz} level readings a second (need {P6.MIN_READ_HZ}): the computer is too busy")
        self.recorder = P6.CamRecorder(cam, tracker=tracker) if record else None
        self._info = {"column": column, "boxes": {k: list(c.roi) for k, c in tracker.columns.items()}, "pours": []}
        return self

    def pour(self, direction: int, target: float, max_tilt: float, view: bool = True, on_tick=None):
        """pour_right / pour_left through arm_server's tilt_stream, to `target` (fraction of full).
        Returns pour_motor6.PourResult."""
        P6 = self.P6
        joint = P6.ServerTiltJoint(6)
        win = P6.PourView(self.cam, self.tracker, self.column) if view else None
        fn = P6.pour_right if direction > 0 else P6.pour_left

        def tick(rec):
            if self.recorder:
                self.recorder.status = rec
            if win:
                win(rec)
            if on_tick:
                on_tick(rec)
        try:
            # the columns around the target are watched for a RISE from their level now (spill from
            # earlier pours is fine): empty=() means none of them has to read zero
            r = fn(joint, self.tracker, self.column, calib_path=None, empty=(),
                   cfg=P6.PourConfig(target_frac=target, max_tilt_deg=max_tilt), on_tick=tick)
            self._info["pours"].append({k: v for k, v in r.__dict__.items() if k != "guide"})
            return r
        finally:
            joint.close()       # ends the tilt_stream: arm_server rolls back to the base angle
            if win:
                win.close()

    def level(self, fresh_s: float = 1.0, column: str | None = None) -> tuple[float, float, float]:
        """(fraction of full, mL, capacity mL) in a column now (default: the pour column)."""
        column = column or self.column
        end = time.time() + fresh_s
        s = self.tracker.read(column)
        while (time.time() - s.ts > 0.5 or s.conf < 0.5) and time.time() < end:
            time.sleep(0.05)
            s = self.tracker.read(column)
        if time.time() - s.ts > 0.5 or s.conf < 0.5:
            raise RuntimeError(f"camera can't read column {column} (conf {s.conf:.2f}): vial, arm or hand in "
                               "view, or too dark")
        frac = float(np.clip(s.px / self.tracker.columns[column].roi[3], 0.0, 1.0))
        return frac, frac * self.capacity_ml, self.capacity_ml

    def pour_ml(self, direction: int, add_ml: float, max_tilt: float, view: bool = True, on_tick=None):
        """Pour add_ml MORE into the column (whatever it already holds). Returns (PourResult, mL added or None).
        Refuses, without tilting, if that would overfill the channel."""
        frac0, ml0, cap = self.level()
        target = frac0 + add_ml / cap
        if target > 0.97:
            raise RuntimeError(f"channel {self.column} holds {ml0:.1f} of {cap:g} mL: +{add_ml:g} mL would overflow "
                               "(past 97 % of the rim)")
        if target < 0.1:
            raise RuntimeError(f"+{add_ml:g} mL into an empty channel is below what the pour can aim at "
                               f"(10 % of the channel = {0.1 * cap:.2f} mL)")
        r = self.pour(direction, target, max_tilt, view=view, on_tick=on_tick)
        added = None if r.final_ml is None else round(r.final_ml - ml0, 3)
        return r, added

    def close(self) -> None:
        if getattr(self, "recorder", None):
            self.recorder.stop(self._info)
            self.recorder = None
        if getattr(self, "owns_camera", True):
            self.tracker.stop()
            self.cam.stop()


def pour_fixed(client: ArmClient, cfg: dict, direction: int, tilt_deg: float, hold_s: float) -> str:
    base = client.start_tilt_stream(int(cfg.get("joint", 6)), float(cfg.get("max_rate_deg_s", 120.0)))
    tilt = min(abs(tilt_deg), roll_room_deg(base, direction) if not client.dry_run else abs(tilt_deg))
    try:
        for a, b, secs in ((0.0, tilt, 1.5), (tilt, tilt, hold_s), (tilt, 0.0, 1.5)):
            n = max(1, int(secs * 30))
            for k in range(1, n + 1):
                client.set_tilt(direction * (a + (b - a) * k / n))
                time.sleep(1 / 30)
    finally:
        client.end_tilt_stream()
    return f"fixed pour: {tilt:.0f} deg for {hold_s:.1f} s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vial")
    ap.add_argument("compartment")
    ap.add_argument("--run", action="store_true", help="move the arm (otherwise plan and print only)")
    ap.add_argument("--pour", choices=["camera", "fixed", "none"], default="camera")
    ap.add_argument("--side", choices=["right", "left"], default=None,
                    help="roll direction (default: layout.yaml pour.direction, +1 = right)")
    ap.add_argument("--cam", default="0", help="camera pour: side camera index or path")
    ap.add_argument("--boxes", default=None, help="camera pour: boxes file (default sense/live_boxes.json)")
    ap.add_argument("--target", type=float, default=0.9, help="camera pour: fill target, fraction of full")
    ap.add_argument("--add-ml", type=float, default=None,
                    help="camera pour: pour this many mL MORE (whatever the channel holds) instead of --target")
    ap.add_argument("--max-tilt", type=float, default=None,
                    help="camera pour: max roll from upright, deg (default layout.yaml pour.max_tilt_deg, 85)")
    ap.add_argument("--no-view", action="store_true", help="camera pour: no live camera window")
    ap.add_argument("--tilt", type=float, default=80.0, help="fixed pour: roll angle, degrees")
    ap.add_argument("--hold", type=float, default=3.0, help="fixed pour: seconds at full tilt")
    a = ap.parse_args()

    layout = load_layout()
    cfg = layout.get("pour") or {}
    try:
        dxy = route(layout, a.vial, a.compartment)
    except (KeyError, ValueError) as e:
        sys.exit(f"NOT MOVING: {e}")
    lift_mm, lower_mm = float(layout.get("lift_mm", 150)), float(layout.get("lower_mm", 110))
    direction = pour_direction(cfg, a.side)
    column = camera_column(cfg, a.compartment)
    max_tilt = a.max_tilt if a.max_tilt is not None else float(cfg.get("max_tilt_deg", 85.0))

    client = ArmClient(dry_run=not a.run)
    try:
        q0 = client.current_joints()
    except RuntimeError as e:
        sys.exit(str(e))
    try:
        out = M.plan_transfer(q0, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
        back = M.plan_return(out[-1].final_joints, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
    except M.PlanError as e:
        sys.exit(f"NOT MOVING: {e}")

    roll = out[-1].final_joints[6]
    room = roll_room_deg(roll, direction)
    print(f"{a.vial} -> {a.compartment}: offset ({dxy[0]:+.0f}, {dxy[1]:+.0f}) mm")
    for p in out + back:
        print("  " + p.summary())
    print(f"  moves total {sum(p.duration_s for p in out + back):.1f} s (+ pour)")
    if a.pour != "none":
        print(f"  pour {'right' if direction > 0 else 'left'} into camera column {column}: wrist roll at "
              f"{roll:+.0f} deg, room {room:.0f} deg to its limit")
        if room < 20:
            sys.exit(f"NOT MOVING: only {room:.0f} deg of wrist roll left to pour "
                     f"{'right' if direction > 0 else 'left'}; re-grab with the roll nearer 0 or pour the other way")
    if not a.run:
        print("Plan only. Add --run to move the arm.")
        return

    camera = None
    if a.pour == "camera":
        try:
            camera = CameraPour(a.cam, column, capacity_ml(layout), boxes=a.boxes)   # checked now, so a bad
            # camera never strands the tube
        except (RuntimeError, ImportError) as e:
            sys.exit(f"NOT MOVING: {e}")
    ok = True
    try:
        for p in out:
            client.run(p)
        if camera:
            if a.add_ml is not None:
                r, added = camera.pour_ml(direction, a.add_ml, max_tilt, view=not a.no_view)
                print(f"added {added} mL (asked {a.add_ml:g})")
            else:
                r = camera.pour(direction, a.target, max_tilt, view=not a.no_view)
            ok = r.ok
            fill = f"{r.final_fill:.0%} of target" if r.final_fill is not None else "fill unknown"
            print(f"pour {r.result}: {r.reason} ({fill})")
        elif a.pour == "fixed":
            print(pour_fixed(client, cfg, direction, a.tilt, a.hold))
        for p in back:
            client.run(p)
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"STOPPED (arm is holding where it is): {e}")
    finally:
        if camera:
            camera.close()
    print("transfer done: tube back in its slot" + ("" if ok else " (the pour did NOT finish, see above)"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
