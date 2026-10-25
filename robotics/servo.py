"""Put the gripper exactly where it was taught relative to a container, wherever the tray is: wrist-camera alignment.

The tray finder (markers.py) maps the scan photo to the table with one homography, good to ~10 mm away from where it
was calibrated: enough to get the gripper to a container, not to pour into one channel. The wrist camera is fixed
to the gripper, so if the tray's markers look the same in the wrist image as they did at a taught pour pose (same
orientation, same height), the gripper is in the same place relative to the tray. No camera model needed.

    teach(key)          at a correct pour pose (teleop holding it): save where markers 10-13 are in the wrist image
                        -> servo_refs.yaml[key]
    align(client, key)  near the pour pose: compare the markers with the reference, move sideways to cancel the
                        difference, repeat until within TOL_PX (or ITERS moves). Returns the total move (mm). The
                        first time, +-STEP_MM test moves measure how the markers move per mm (saved).

    python servo.py teach b --run        wrist_stream.py running, gripper at b's pour pose
    python servo.py align b --run        align on b from where the arm is now
    python servo.py show                 the saved references

Units: mm in the base frame (x forward, y left, z up); pixels in the 640x480 wrist image.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import yaml

import markers as T
import motion as M

HERE = Path(__file__).resolve().parent
REFS = HERE / "servo_refs.yaml"
STEP_MM = 6.0         # teach: test moves for the image Jacobian
TOL_PX = 1.5          # align: done when the markers are this close to the reference (mean over markers)
ITERS = 6             # align: at most this many corrective moves
MAX_STEP_MM = 15.0    # align: never move more than this in one go
MAX_TOTAL_MM = 40.0   # align: refuse to wander further than this from where it started
FRAMES = 3            # median over this many fresh frames per look
SETTLE_S = 0.6


def look(n: int = FRAMES) -> dict[int, np.ndarray]:
    """{marker id: centre pixel} as the median over n fresh wrist frames (wrist_stream.py writes them)."""
    import cv2
    seen: dict[int, list] = {}
    last = 0.0
    t_end = time.time() + 4.0
    while time.time() < t_end and min((len(v) for v in seen.values()), default=0) < n:
        try:
            m = T.STREAM_FILE.stat().st_mtime
        except OSError:
            m = 0.0
        if m > last:
            last = m
            frame = cv2.imread(str(T.STREAM_FILE))
            if frame is not None:
                for i, c in T.detect_markers(frame):
                    if i in T.MARKERS:
                        seen.setdefault(i, []).append(T.marker_center(c))
                if not seen:
                    n_tries = getattr(look, "_empty", 0) + 1
                    look._empty = n_tries
        time.sleep(0.03)
    return {i: np.median(np.array(v), axis=0) for i, v in seen.items() if len(v) >= max(1, n - 1)}


def _move_by(client, d_xy, label: str) -> None:
    q = client.current_joints()
    p, _ = M.fk_mm(q)
    goal = p + np.array([d_xy[0], d_xy[1], 0.0])
    client.run(M.plan_line(q, goal, None, speed_mm_s=10.0, label=label))
    time.sleep(SETTLE_S)


def load_refs() -> dict:
    return (yaml.safe_load(REFS.read_text()) or {}) if REFS.exists() else {}


def save_ref(key: str, entry: dict) -> None:
    refs = load_refs()
    refs[key] = {**refs.get(key, {}), **entry}
    REFS.write_text("# wrist-camera references for servo.py align: where the tray's markers appear at a taught pour "
                    "pose, and how they move per mm" + chr(10) + yaml.safe_dump(refs, sort_keys=False, width=120))


def teach(key: str, joints: dict | None = None) -> dict:
    """Save where the tray's markers are in the wrist image right now (the gripper at a correct pour pose, e.g.
    held there by teleop). No arm moves: the image Jacobian is measured by align the first time it runs."""
    ref = look()
    if len(ref) < 2:
        raise RuntimeError(f"only markers {sorted(ref)} in the wrist view here: need 2+ in view at the pour pose")
    entry = {"taught": time.strftime("%Y-%m-%d %H:%M:%S"),
             "markers_px": {int(i): [round(float(v), 2) for v in ref[i]] for i in sorted(ref)}}
    if joints:
        entry["pose_joints"] = {int(j): round(float(v), 2) for j, v in joints.items()}
    refs = load_refs()
    if key in refs:
        refs[key].pop("jacobian_px_per_mm", None)   # a new reference: measure the Jacobian again
        REFS.write_text("# wrist-camera references" + chr(10) + yaml.safe_dump(refs, sort_keys=False, width=120))
    save_ref(key, entry)
    return entry


def measure_jacobian(client, key: str) -> np.ndarray:
    """Move the gripper +-STEP_MM in x and y (orientation and height kept) and back: how the markers move in the
    image per mm. Saved with the reference (it depends on the pour pose, not on where the tray is)."""
    ref = look()
    cols = []
    for axis in (0, 1):
        shifts = []
        for sign in (1, -1):
            d = np.zeros(2)
            d[axis] = sign * STEP_MM
            _move_by(client, d, f"jacobian: {'xy'[axis]} {sign * STEP_MM:+g} mm")
            now = look()
            _move_by(client, -d, "jacobian: back")
            common = [i for i in ref if i in now]
            if common:
                shifts.append(sign * np.mean([now[i] - ref[i] for i in common], axis=0) / STEP_MM)
        if not shifts:
            raise RuntimeError("markers lost during the test moves")
        cols.append(np.mean(shifts, axis=0))
    J = np.column_stack(cols)                     # px per mm: [du/dx du/dy; dv/dx dv/dy]
    if abs(np.linalg.det(J)) < 1e-3:
        raise RuntimeError(f"image Jacobian is degenerate: {J.round(3).tolist()}")
    save_ref(key, {"jacobian_px_per_mm": [[round(float(v), 4) for v in row] for row in J]})
    return J


def align(client, key: str, log=print) -> np.ndarray:
    """Move sideways until the markers look as taught for `key`. Returns the total move (mm, base frame)."""
    refs = load_refs()
    if key not in refs:
        raise RuntimeError(f"no wrist-camera reference for {key!r}: python servo.py teach {key} --run")
    ref = {int(i): np.asarray(v, float) for i, v in refs[key]["markers_px"].items()}
    J = refs[key].get("jacobian_px_per_mm")
    J = np.asarray(J, float) if J else measure_jacobian(client, key)
    Jinv = np.linalg.inv(J)
    total = np.zeros(2)
    for k in range(ITERS + 1):
        now = look()
        common = [i for i in ref if i in now]
        if not common:
            raise RuntimeError(f"none of the reference markers {sorted(ref)} in the wrist view (saw {sorted(now)})")
        err = np.mean([ref[i] - now[i] for i in common], axis=0)          # px to go
        e = float(np.linalg.norm(err))
        log(f"    wrist align {k}: markers {common} off by {e:.1f} px ({err[0]:+.1f}, {err[1]:+.1f})")
        if e <= TOL_PX or k == ITERS:
            break
        d = Jinv @ err
        n = float(np.linalg.norm(d))
        if n > MAX_STEP_MM:
            d *= MAX_STEP_MM / n
        if np.linalg.norm(total + d) > MAX_TOTAL_MM:
            raise RuntimeError(f"wrist align would move {np.linalg.norm(total + d):.0f} mm from the start "
                               f"(> {MAX_TOTAL_MM:g}): wrong reference or markers?")
        _move_by(client, d, f"wrist align ({d[0]:+.1f}, {d[1]:+.1f}) mm")
        total += d
    log(f"    wrist align: moved ({total[0]:+.1f}, {total[1]:+.1f}) mm in total")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["teach", "align", "show"])
    ap.add_argument("key", nargs="?", help="container letter (a-d), optionally with the vial: b or b:vial_B")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "show":
        print(yaml.safe_dump(load_refs(), sort_keys=False))
        return
    if not a.key or not a.run:
        raise SystemExit("teach / align KEY --run (arm_server.py and wrist_stream.py running)")
    from arm_client import ArmClient
    c = ArmClient() if a.cmd == "align" else None
    if a.cmd == "teach":
        e = teach(a.key)
        print(f"taught {a.key}: markers {sorted(e['markers_px'])}")
    else:
        align(c, a.key)


if __name__ == "__main__":
    main()
