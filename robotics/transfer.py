"""Vial -> compartment -> vial round trip with a camera-steered pour, from the pose a grab ends in.

    lift 100 -> shift (dx, dy) in that locked horizontal plane -> lower 60
    -> camera pour on the wrist roll (software/laminar/pour_motor6.py: pour_right / pour_left)
    -> lift 60 -> shift (-dx, -dy) -> lower 100

(dx, dy) comes from layout.yaml (the only bench measurements needed). All six moves are planned, and the
camera calibration and the camera checked, before the arm moves; each move is played continuously by
arm_server.py ("path" op). The grab before and the release after are separate (recorded) steps.

The pour fills the compartment's camera column (layout.yaml pour.columns) to --target of full, slowing as
it fills, and tilts back upright when done or on wrong column / overfill / camera lost / no flow. The tube
is carried back in every case, except an error from the arm itself (then it holds where it is).

Run from robotics/ with arm_server.py running and the tube already gripped in its slot. Calibrate the
side camera first:  cd software && python -m laminar.pour_motor6 calibrate --cam <index>

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
DEFAULT_CALIB = HERE.parent / "sense" / "level_calib.json"
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


def camera_column(cfg: dict, compartment: str) -> str:
    """The side-camera column (as named in the camera calibration) that shows this compartment."""
    return str((cfg.get("columns") or {}).get(compartment, compartment))


class CameraPour:
    """Side camera + level tracker for pour_motor6, opened and checked BEFORE the arm moves."""

    def __init__(self, cam: str, calib: str | FilePath, column: str):
        from laminar import pour_motor6 as P6
        problems = P6.check_calibration(calib, column)
        if problems:
            raise RuntimeError("camera not calibrated: " + "; ".join(problems)
                               + "  (run: cd software && python -m laminar.pour_motor6 calibrate)")
        from cameras import LatestFrame, open_camera
        from level import LevelTracker, load_columns
        self.P6, self.calib, self.column = P6, str(calib), column
        self.cam = LatestFrame(open_camera(int(cam) if str(cam).isdigit() else cam)).start()
        self.tracker = LevelTracker(self.cam, load_columns(self.calib))
        frame, _ = self.cam.read()
        end = time.time() + 3
        while frame is None and time.time() < end:
            time.sleep(0.05)
            frame, _ = self.cam.read()
        self.tracker.ref = P6._load_reference(self.calib, frame.shape if frame is not None else None)
        if self.tracker.ref is None:
            self.cam.stop()
            raise RuntimeError(f"camera {cam}: no frames, or the empty reference image was taken with another "
                               "camera / resolution. Wrong --cam, or recalibrate.")
        self.tracker.start()
        self.empty = json.loads(FilePath(self.calib).read_text()).get("empty", [])

    def pour(self, direction: int, target: float, max_tilt: float, view: bool = True, on_tick=None):
        """pour_right / pour_left through arm_server's tilt_stream. Returns pour_motor6.PourResult."""
        P6 = self.P6
        joint = P6.ServerTiltJoint(6)
        win = P6.PourView(self.cam, self.tracker, self.column, self.empty) if view else None
        fn = P6.pour_right if direction > 0 else P6.pour_left

        def tick(rec):
            if win:
                win(rec)
            if on_tick:
                on_tick(rec)
        try:
            return fn(joint, self.tracker, self.column, calib_path=self.calib,
                      cfg=P6.PourConfig(target_frac=target, max_tilt_deg=max_tilt), on_tick=tick)

        finally:
            joint.close()       # ends the tilt_stream: arm_server rolls back to the base angle
            if win:
                win.close()

    def level(self, fresh_s: float = 1.0) -> tuple[float, float, float]:
        """(fraction of full, mL, capacity mL) in the pour column now, from the percent calibration."""
        calib = json.loads(FilePath(self.calib).read_text())
        curve = (calib.get("fill_curve") or {}).get(self.column)
        if not curve or len(curve) < 2 or not calib.get("capacity_ml"):
            raise RuntimeError("camera calibration has no fill curve / capacity: recalibrate with --percent "
                               "--capacity-ml <mL in a full channel>")
        end = time.time() + fresh_s
        s = self.tracker.read(self.column)
        while (time.time() - s.ts > 0.5 or s.conf < 0.5) and time.time() < end:
            time.sleep(0.05)
            s = self.tracker.read(self.column)
        if time.time() - s.ts > 0.5 or s.conf < 0.5:
            raise RuntimeError(f"camera can't read column {self.column} (conf {s.conf:.2f}): vial, arm or hand in "
                               "view, or too dark")
        pxs, fracs = zip(*sorted(curve))
        frac = float(np.clip(np.interp(s.px, pxs, fracs), 0.0, 1.0))
        cap = float(calib["capacity_ml"])
        return frac, frac * cap, cap

    def pour_ml(self, direction: int, add_ml: float, max_tilt: float, view: bool = True, on_tick=None):
        """Pour add_ml MORE into the column (whatever it already holds). Returns (PourResult, mL added or None).
        Refuses, without tilting, if that would overfill the channel."""
        frac0, ml0, cap = self.level()
        target = frac0 + add_ml / cap
        if target > 1.0:
            raise RuntimeError(f"channel {self.column} holds {ml0:.1f} of {cap:g} mL: +{add_ml:g} mL would overflow")
        r = self.pour(direction, target, max_tilt, view=view, on_tick=on_tick)
        added = None if r.final_ml is None else round(r.final_ml - ml0, 3)
        return r, added

    def close(self) -> None:
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
    ap.add_argument("--calib", default=str(DEFAULT_CALIB), help="camera pour: camera calibration file")
    ap.add_argument("--target", type=float, default=0.9, help="camera pour: fill target, fraction of full")
    ap.add_argument("--add-ml", type=float, default=None,
                    help="camera pour: pour this many mL MORE (whatever the channel holds) instead of --target")
    ap.add_argument("--max-tilt", type=float, default=85.0, help="camera pour: max roll from upright, deg")
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
            camera = CameraPour(a.cam, a.calib, column)   # checked now, so a bad camera never strands the tube
        except (RuntimeError, ImportError) as e:
            sys.exit(f"NOT MOVING: {e}")
    ok = True
    try:
        for p in out:
            client.run(p)
        if camera:
            if a.add_ml is not None:
                r, added = camera.pour_ml(direction, a.add_ml, a.max_tilt, view=not a.no_view)
                print(f"added {added} mL (asked {a.add_ml:g})")
            else:
                r = camera.pour(direction, a.target, a.max_tilt, view=not a.no_view)
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
