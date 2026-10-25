"""Find the container tray with the wrist camera and drive the gripper above each container.

The tray: the 4 containers are stuck together and always move as one. Each has an ArUco marker (DICT_4X4_50,
ids 10-13 = containers 1-4, 20 mm black square) lying flat on the table beside it. Judges will move the tray, so nothing here
hardcodes where it is: one photo from the SCAN POSE (arm high, wrist camera seeing the whole tray) says where.

How, in plain words
    1. Calibrate once (`calibrate`, step by step). Pick a SCAN POSE: arm high, wrist camera seeing the tray from
       above. Put the tray somewhere, touch the centre of each marker with the gripper tip (forward kinematics of
       the follower's joints gives each marker's (x, y) in mm), then let arm_server.py drive to the scan pose
       (base turned towards the tray) and take a photo. Repeat at 2+ tray positions spread over the area.
    2. From those: a homography (pixel -> table mm, valid for photos taken from the scan pose) fitted to every
       marker corner (markers are 20 mm squares, square to the tray line), the table height, and the tray LAYOUT:
       where each marker sits in the tray's own frame (origin = the markers' centroid, x axis = from marker 10
       towards marker 13).
    3. Afterwards one photo from the scan pose finds the tray: every visible marker's centre goes through the
       homography, and a rigid fit (rotation + translation, no scaling) of the layout onto those points gives the
       tray's (x, y, heading). Two visible markers are enough, so one or two can be hidden.
    4. Each container's position = the tray pose applied to its marker's layout position, hidden ones included.

Learned on the real arm: photos must be taken UNDER POWER (arm_server holding the scan pose): the joints sag
~2 deg under load, enough to move the view by centimetres, so a hand-held photo maps differently. The scan pose
sees a band of distances from the base (about 450-570 mm for the first one); turning the base sweeps the band
sideways but can't bring the camera closer. The map is only good where it was calibrated: spread the tray
positions over the area the tray may be in.

Commands (degrees for joints, mm in the arm base frame: x forward, y left, z up)
    python markers.py view                  live wrist camera: marker ids + the fitted tray outline. Never moves
                                            the arm; runs alongside teleop.py.
    python markers.py calibrate STEP        scan-pose | touch POS ID | photo POS [DJ1] | shape POS | finish | reset
                                            per call (see cmd_calibrate); finish writes the `tray` section of
                                            vision_calib.yaml and prints the error in mm
    python markers.py visit [--run]         arm_server.py running: scan pose -> find the tray -> for each container
                                            lift, travel, lower to HOVER_MM (50 mm) above its marker, gripper pointing
                                            down; Enter before every move. Without --run: plan and print only.
Joints come from teleop_state.json while teleop.py runs (it writes it at 10 Hz), else straight from the
encoders with the motors off (the arm moved by hand).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

import motion as M
from vision import rotate_about_base

HERE = Path(__file__).resolve().parent
CALIB_FILE = HERE / "vision_calib.yaml"     # shared with vision.py; this module owns the `tray` section
TELEOP_STATE = HERE / "teleop_state.json"   # follower joints written by teleop.py while it runs
STREAM_FILE = HERE / "wrist_latest.jpg"     # latest frame from wrist_stream.py, when it owns the camera
SESSION_FILE = HERE / "tray_calib_session.json"  # calibrate's steps so far
BENCH_FILE = HERE / "bench.yaml"

MARKERS = {10: 1, 11: 2, 12: 3, 13: 4}      # ArUco id -> container number
MARKER_MM = 20.0       # side of a marker's black square (measured on the tray)
HOVER_MM = 50.0        # visit: gripper tip ends this far above the marker centre (--hover)
CARRY_MM = 60.0        # ... and travels between containers this much higher than that
CAMERA = 2             # wrist camera, DirectShow index (indices shift after re-plugs: --camera)
TOUCH_MAX_Z_MM = 100.0  # calibrate touch: the tip counts as on the table below this height
STATE_MAX_AGE_S = 1.0  # teleop_state.json older than this = teleop.py is not running
MOVE_SPEED = 15.0      # deg/s for the joint move to the scan pose
SWEEP_DEG = (0, 25, -25, 50, -50)  # visit: base turns (J1, from the scan pose) tried until the tray is seen
LOCK_PHOTOS = 2        # visit: photos at the angle where the tray is first seen; their poses are averaged
CORRECT_TOL_MM = 1.0   # visit: after each lower, nudge until the MEASURED tip is this close to the target ...
CORRECT_ITERS = 5      # ... in at most this many small moves ...
CORRECT_MAX_MM = 60.0  # ... never commanding the tip further than this from where it was planned


def detect_markers(image) -> list[tuple[int, np.ndarray]]:
    """[(id, 4x2 corner pixels)] of every DICT_4X4_50 marker. Full error correction: on the real tray, marker 12
    seen small and at an angle from the scan pose has a bit or two misread and the default rejects it; a wrong
    id is still unlikely, since only ids 10-13 count and the rigid fit must agree."""
    a = cv2.aruco
    params = a.DetectorParameters()
    params.errorCorrectionRate = 1.0
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = a.ArucoDetector(a.getPredefinedDictionary(a.DICT_4X4_50), params).detectMarkers(gray)
    return [] if ids is None else [(int(i), c.reshape(4, 2).astype(float)) for i, c in zip(ids.flatten(), corners)]


# =============================================================================================
# Geometry (pure functions: tested without hardware)
# =============================================================================================

def marker_center(corners) -> np.ndarray:
    """Pixel centre of a marker = where its diagonals cross (exact under perspective, unlike the corner mean)."""
    c = np.asarray(corners, dtype=float)
    p, r = c[0], c[2] - c[0]
    q, s = c[1], c[3] - c[1]
    t = np.cross(q - p, s) / np.cross(r, s)
    return p + t * r


def px_to_mm(H, px) -> np.ndarray:
    """Pixels (N x 2) -> table (x, y) mm through homography H."""
    pts = np.asarray(px, dtype=float).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, np.asarray(H, dtype=float)).reshape(-1, 2)


def mm_to_px(H, xy) -> np.ndarray:
    pts = np.asarray(xy, dtype=float).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, np.linalg.inv(np.asarray(H, dtype=float))).reshape(-1, 2)


def fit_homography(px, xy) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares homography from >= 4 pixel / table-mm pairs. Returns (H, error per point in mm)."""
    px, xy = np.asarray(px, dtype=float), np.asarray(xy, dtype=float)
    H, _ = cv2.findHomography(px, xy, 0)   # 0 = plain least squares over every point (no RANSAC)
    if H is None:
        raise ValueError("homography failed: are the touched points all on one line?")
    return H, np.linalg.norm(px_to_mm(H, px) - xy, axis=1)


def rigid_fit(src, dst) -> tuple[float, np.ndarray, float]:
    """Rotation + translation (2-D, no scale) that best maps src points onto dst (Kabsch).
    Returns (angle deg, translation (2,), RMS residual mm). Needs >= 2 points."""
    src, dst = np.asarray(src, dtype=float), np.asarray(dst, dtype=float)
    if len(src) < 2:
        raise ValueError("a rigid fit needs at least 2 points")
    cs, cd = src.mean(axis=0), dst.mean(axis=0)
    A, B = src - cs, dst - cd
    ang = math.atan2(float(np.sum(A[:, 0] * B[:, 1] - A[:, 1] * B[:, 0])), float(np.sum(A * B)))
    R = rot(math.degrees(ang))
    t = cd - R @ cs
    rms = float(np.sqrt(np.mean(np.sum((src @ R.T + t - dst) ** 2, axis=1))))
    return math.degrees(ang), t, rms


def rot(deg: float) -> np.ndarray:
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])


def tray_layout(xy_by_id: dict[int, np.ndarray]) -> dict[int, np.ndarray]:
    """Marker centres in the tray's own frame: origin at their centroid, x axis from the first id to the last."""
    ids = sorted(xy_by_id)
    P = np.array([xy_by_id[i] for i in ids], dtype=float)
    c = P.mean(axis=0)
    d = P[-1] - P[0]
    R = rot(math.degrees(math.atan2(d[1], d[0])))
    return {i: R.T @ (p - c) for i, p in zip(ids, P)}


def tray_pose(layout: dict[int, np.ndarray], seen_xy: dict[int, np.ndarray]) -> dict:
    """Fit the tray onto the markers that were seen (>= 2). Returns {"xy", "heading_deg", "rms_mm", "seen",
    "containers": {container: (x, y) mm}}, every container placed, hidden ones too."""
    ids = [i for i in sorted(seen_xy) if i in layout]
    if len(ids) < 2:
        raise ValueError(f"need at least 2 tray markers, saw {ids or 'none'}")
    ang, t, rms = rigid_fit([layout[i] for i in ids], [seen_xy[i] for i in ids])
    R = rot(ang)
    return {"xy": t, "heading_deg": ang, "rms_mm": rms, "seen": ids,
            "containers": {MARKERS[i]: R @ np.asarray(layout[i]) + t for i in sorted(layout)}}


def find_tray(frame, calib: dict, dj1: float = 0.0) -> dict:
    """One scan-pose photo, taken with the base turned dj1 deg from the scan pose -> tray pose (see tray_pose),
    plus "px" = the pixel centre of each seen marker."""
    px = {i: marker_center(c) for i, c in detect_markers(frame) if i in MARKERS}
    if not px:
        raise ValueError("no tray markers in the picture")
    mm = rotate_about_base(px_to_mm(calib["homography"], list(px.values())), dj1)
    xy = dict(zip(px, mm))
    pose = tray_pose(calib["layout_mm"], xy)
    pose["px"] = px
    return pose


def lock_in(looks: list[dict]) -> dict:
    """Average tray detections taken from the same base angle into one locked pose: each container's position is
    the mean over the photos; "spread_mm" = how far the photos disagree (worst container)."""
    conts = {k: np.mean([lk["containers"][k] for lk in looks], axis=0) for k in looks[0]["containers"]}
    spread = max(float(np.linalg.norm(lk["containers"][k] - conts[k])) for lk in looks for k in conts)
    best = max(looks, key=lambda lk: len(lk["seen"]))
    return {**best, "containers": conts, "spread_mm": spread,
            "seen": sorted({i for lk in looks for i in lk["seen"]}),
            "xy": np.mean([lk["xy"] for lk in looks], axis=0)}


SQUARE = np.array([[-1, 1], [1, 1], [1, -1], [-1, -1]], float)  # ArUco corner order, seen from above


def marker_corners_mm(center, line_deg: float, size: float = MARKER_MM) -> np.ndarray:
    """Table (x, y) of a marker's 4 corners in ArUco order. The markers sit square to the tray line (marker 10 ->
    13, measured on the real tray), so the corners follow from the touched centre and that line."""
    return np.asarray(center, dtype=float) + (size / 2) * SQUARE @ rot(line_deg).T


def calibrate_from(touches: list[dict], photos: list[dict]) -> tuple[dict, dict]:
    """touches: [{"pos", "id", "xy_mm", "z_mm"}] for 1+ touched tray positions; photos: [{"pos", "corners":
    {id: 4x2 px}, "dj1"?: deg}], one per position, all from the scan pose UNDER POWER (the joints sag ~2 deg under
    load, so a hand-held photo sees from a different place), the base possibly turned by dj1 -> (calib, report).

    Two kinds of photo pin down the homography (pixel -> table mm):
      - a TOUCHED position: every marker corner seen is a pixel / table-mm pair (corners from the touched centre,
        the 20 mm marker size and the tray line);
      - an UNTOUCHED position (no touches for that pos): the tray's position and heading there are unknowns, and
        every corner must land on the tray's known shape (layout + 20 mm squares). No touching needed, so the tray
        can be photographed at many spots to make the map good everywhere it may be.
    Both are solved together (least squares). A photo taken with the base turned by dj1 is turned back by -dj1."""
    at = {}
    for t in touches:
        at.setdefault(t["pos"], {})[t["id"]] = np.asarray(t["xy_mm"], dtype=float)
    # the tray layout from the touched positions (first one, the others fitted onto it and averaged in)
    ref, others = min(at), [p for p in sorted(at) if p != min(at)]
    layout = tray_layout(at[ref])
    rigid = [tray_pose(layout, at[p])["rms_mm"] for p in others if len(at[p]) >= 2]
    sums = {i: [v] for i, v in layout.items()}
    for p in others:
        ang, t, _ = rigid_fit([layout[i] for i in sorted(at[p])], [at[p][i] for i in sorted(at[p])])
        for i in at[p]:
            sums[i].append(rot(ang).T @ (at[p][i] - t))
    layout = {i: np.mean(v, axis=0) for i, v in sums.items()}
    shape = {i: layout[i] + (MARKER_MM / 2) * SQUARE for i in layout}  # corners in the tray frame (x = tray line)

    touched = [ph for ph in photos if ph["pos"] in at]
    untouched = [ph for ph in photos if ph["pos"] not in at]
    px, xy = [], []
    for ph in touched:
        pts = at[ph["pos"]]
        d = pts[max(pts)] - pts[min(pts)]
        line = math.degrees(math.atan2(d[1], d[0]))
        for i, c in ph["corners"].items():
            px.append(np.asarray(c, dtype=float))
            xy.append(rotate_about_base(marker_corners_mm(pts[i], line), -ph.get("dj1", 0.0)))
    H, _ = fit_homography(np.vstack(px), np.vstack(xy))

    def mapped(Hm, ph, ids):
        return [rotate_about_base(px_to_mm(Hm, ph["corners"][i]), ph.get("dj1", 0.0)) for i in ids]

    if untouched:
        from scipy.optimize import least_squares
        poses0 = []
        for ph in untouched:  # start each tray pose from the touched-only map
            ids = sorted(ph["corners"])
            c = [m.mean(axis=0) for m in mapped(H, ph, ids)]
            ang, t, _ = rigid_fit([layout[i] for i in ids], c)
            poses0 += [math.radians(ang), t[0], t[1]]

        def unpack(x):
            return np.append(x[:8], 1.0).reshape(3, 3), x[8:].reshape(-1, 3)

        def resid(x):
            Hm, poses = unpack(x)
            out = [(px_to_mm(Hm, np.vstack(px)) - np.vstack(xy)).ravel()]  # touched corners, scan-pose frame
            for ph, (a, tx, ty) in zip(untouched, poses):               # untouched: the tray's known shape
                R = rot(math.degrees(a))
                for i, m in zip(sorted(ph["corners"]), mapped(Hm, ph, sorted(ph["corners"]))):
                    out.append((m - (shape[i] @ R.T + [tx, ty])).ravel())
            return np.concatenate(out)

        x0 = np.concatenate([(H / H[2, 2]).ravel()[:8], poses0])
        H = unpack(least_squares(resid, x0, x_scale="jac").x)[0]

    # how good is it: touched marker centres vs. the map; untouched photos: corners vs. the tray's true shape,
    # and the 20 mm marker sides as the map measures them
    points = {}
    for ph in touched:
        for i, c in ph["corners"].items():
            got = rotate_about_base(px_to_mm(H, [marker_center(c)])[0], ph.get("dj1", 0.0))
            points[f"tray {ph['pos']} marker {i}"] = float(np.linalg.norm(got - at[ph["pos"]][i]))
    shape_err, sides = [], []
    for ph in untouched:
        ids = sorted(ph["corners"])
        m = mapped(H, ph, ids)
        ang, t, _ = rigid_fit(np.vstack([shape[i] for i in ids]), np.vstack(m))
        R = rot(ang)
        shape_err += [float(np.linalg.norm(p - (q @ R.T + t))) for i, mm in zip(ids, m)
                      for p, q in zip(mm, shape[i])]
        sides += [float(np.linalg.norm(mm[k] - mm[(k + 1) % 4])) for mm in m for k in range(4)]
    zs = [t["z_mm"] for t in touches]
    e = np.array(list(points.values()))
    calib = {"homography": H, "layout_mm": layout, "table_z_mm": float(np.mean(zs))}
    report = {"points": points, "homography_rms_mm": float(np.sqrt(np.mean(e ** 2))),
              "homography_max_mm": float(e.max()),
              "shape_rms_mm": float(np.sqrt(np.mean(np.square(shape_err)))) if shape_err else None,
              "marker_side_mm": (float(min(sides)), float(max(sides))) if sides else None,
              "tray_rigid_rms_mm": float(np.mean(rigid)) if rigid else None,
              "table_z_spread_mm": float(max(zs) - min(zs))}
    return calib, report


# =============================================================================================
# Calibration file (`tray` section of vision_calib.yaml; vision.py's sections are kept)
# =============================================================================================

def save_calib(section: dict, path: Path = CALIB_FILE) -> None:
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    data["tray"] = section
    path.write_text("# vision_calib.yaml: vision.py (lens, table) and markers.py (tray). Do not edit by hand.\n"
                    + yaml.safe_dump(data, sort_keys=False))


def load_calib(path: Path = CALIB_FILE) -> dict:
    """The `tray` section with numpy arrays: homography (3x3), layout_mm {id: (2,)}, scan_pose {1..6: deg}."""
    try:
        t = (yaml.safe_load(path.read_text()) or {})["tray"]
    except (OSError, KeyError, TypeError):
        raise SystemExit("no tray calibration yet: run python markers.py calibrate") from None
    return {**t, "homography": np.array(t["homography"]),
            "layout_mm": {int(i): np.array(v) for i, v in t["layout_mm"].items()},
            "scan_pose": {int(j): float(v) for j, v in t["scan_pose"].items()}}


# =============================================================================================
# Hardware helpers
# =============================================================================================

class _Stream:
    """Frames from wrist_stream.py (it owns the camera; Windows allows one process per camera)."""

    def read(self):
        if time.time() - STREAM_FILE.stat().st_mtime > 1.0:
            return False, None
        frame = cv2.imread(str(STREAM_FILE))
        return frame is not None, frame

    def release(self):
        pass


def open_camera(index: int):
    """wrist_stream.py's frames when it is streaming, else the camera itself."""
    if STREAM_FILE.exists() and time.time() - STREAM_FILE.stat().st_mtime < 1.0:
        return _Stream()
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)   # 640x480 gives 30 fps on the U20CAM (TELEOP.md)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        raise SystemExit(f"camera {index} would not open (indices shift after re-plugs: try --camera)")
    return cap


def grab(cap, settle: int = 1):
    ok, frame = False, None
    for _ in range(settle):  # the first frames after opening are dark while exposure settles
        ok, frame = cap.read()
        if isinstance(cap, _Stream):
            break
    if not ok:
        raise SystemExit("camera returned no frame")
    return frame


def teleop_joints() -> dict[int, float]:
    """The follower's measured joints {1..6: deg} from teleop_state.json (teleop.py must be running)."""
    try:
        st = json.loads(TELEOP_STATE.read_text())
    except (OSError, ValueError):
        raise RuntimeError("no teleop_state.json: is teleop.py running?") from None
    age = time.time() - st["t"]
    if age > STATE_MAX_AGE_S:
        raise RuntimeError(f"teleop_state.json is {age:.0f} s old: is teleop.py running?")
    return {int(j): float(v) for j, v in st["joints"].items() if int(j) <= 6}


def draw(frame, calib: dict | None):
    """Marker ids, and (with a calibration) the fitted tray outline + container numbers. Returns (image, pose)."""
    img = frame.copy()
    found = detect_markers(frame)
    for i, c in found:
        cv2.polylines(img, [c.astype(np.int32)], True, (0, 255, 0) if i in MARKERS else (0, 160, 255), 2)
        u, v = marker_center(c)
        cv2.putText(img, str(i), (int(u) - 8, int(v) + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    pose = None
    if calib:
        try:
            dj1 = teleop_joints()[1] - calib["scan_pose"][1]  # alongside teleop: the base may be turned
        except RuntimeError:
            dj1 = 0.0
        try:
            pose = find_tray(frame, calib, dj1)
        except ValueError:
            pass
    if pose:
        cxy = [pose["containers"][MARKERS[i]] for i in sorted(calib["layout_mm"])]
        cv2.polylines(img, [mm_to_px(calib["homography"], cxy).astype(np.int32)], False, (255, 200, 0), 2)
        for (k, xy), (u, v) in zip(pose["containers"].items(), mm_to_px(calib["homography"], cxy)):
            cv2.putText(img, f"C{k}", (int(u) + 10, int(v) - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 2)
        txt = (f"tray ({pose['xy'][0]:.0f}, {pose['xy'][1]:.0f}) mm  {pose['heading_deg']:.1f} deg  "
               f"fit {pose['rms_mm']:.1f} mm  seen {pose['seen']}")
        cv2.putText(img, txt, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
    return img, pose


# =============================================================================================
# Commands
# =============================================================================================

def cmd_view(a) -> None:
    try:
        calib = load_calib()
    except SystemExit:
        calib = None  # not calibrated yet: show marker ids only
    cap = open_camera(a.camera)
    print("view: q / Esc to quit" + ("" if calib else " (no tray calibration yet: ids only)"))
    try:
        while True:
            img, _ = draw(grab(cap), calib)
            cv2.imshow("markers", img)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


_BUS = None


def hand_joints() -> dict[int, float]:
    """Follower joints {1..6: deg} straight from the motor encoders while the motors are off and the arm is moved
    by hand. Two reads 0.3 s apart must agree within 0.5 deg (a stale read or a moving arm is refused). The CAN
    adapter is opened once per process: Windows refuses a second open right after a close."""
    global _BUS
    from leader_arm import load_follower_zero
    from robstride_can import P_MECH_POS, RobStrideBus
    zero = load_follower_zero()
    if _BUS is None:
        import atexit
        _BUS = RobStrideBus()
        atexit.register(_BUS.close)
    _BUS.drain(0.1)

    def once():
        q = {}
        for mid in range(1, 7):
            pos = _BUS.read_param_f32(mid, P_MECH_POS, timeout=0.2)
            if pos is None:
                raise RuntimeError(f"J{mid} did not answer")
            d = math.degrees(pos) - zero[mid]
            q[mid] = d - 360.0 * round(d / 360.0)  # single-turn wrap after power-up (J6 reads ~+-180 at rest)
        return q
    a = once()
    time.sleep(0.3)
    b = once()
    if max(abs(a[j] - b[j]) for j in a) > 0.5:
        raise RuntimeError("arm still moving")
    return b


def current_joints() -> dict[int, float]:
    """teleop.py's follower joints while it runs, else the encoders (arm moved by hand, motors off)."""
    try:
        return teleop_joints()
    except RuntimeError:
        return hand_joints()


def outside_limits(q) -> list[str]:
    return [f"J{j} {q[j]:.0f} (limits {lo:g}..{hi:g})" for j, (lo, hi) in M.JOINT_LIMITS_DEG.items()
            if not lo - 0.5 <= q[j] <= hi + 0.5]


def _session() -> dict:
    return json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else {"touches": [], "photos": []}


def cmd_calibrate(a) -> None:
    """One step per call, so it can be driven from a chat or a second terminal:
        scan-pose            teleop / hold the arm where the camera sees the tray from above: saved as the scan pose
        touch POS ID         hold the tip on marker ID of tray position POS: recorded when it sits still on the
                             table (no typing while holding the arm; waits up to --wait s)
        photo POS [DJ1]      arm_server.py running, hands off: drives to the scan pose with the base turned towards
                             tray POS (or by DJ1 deg) and takes the photo
        shape POS            arm_server.py running: a photo of the tray at a new, UNTOUCHED spot (finds it itself);
                             its known shape tightens the map there. Do several, spread over the area.
        finish               fit, print the error per marker, save the `tray` section of vision_calib.yaml
        reset                start over
    Use 2+ tray positions spread over the area the tray may be in; every photo is taken under power."""
    s = _session()
    step, args = a.step, a.args
    if step == "reset":
        SESSION_FILE.unlink(missing_ok=True)
        print("calibration session cleared")
        return
    if step == "scan-pose":
        q = current_joints()
        bad = outside_limits(q)
        if bad:
            raise SystemExit("NOT SAVED: arm_server and motion.py refuse a pose outside the joint limits: "
                             + ", ".join(bad))
        s["scan_pose"] = {str(j): round(v, 2) for j, v in q.items()}
        print("scan pose saved: " + "  ".join(f"J{j}={v:.1f}" for j, v in q.items()))
    elif step == "touch":
        pos, mid = int(args[0]), int(args[1])
        end = time.time() + a.wait
        while True:
            try:
                q = current_joints()
                p, _ = M.fk_mm(q)
                if p[2] < TOUCH_MAX_Z_MM:
                    break
            except RuntimeError:
                pass
            if time.time() > end:
                raise SystemExit("timed out: the tip never sat still low on the table")
            time.sleep(0.2)
        s["touches"] = [t for t in s["touches"] if (t["pos"], t["id"]) != (pos, mid)]
        s["touches"].append({"pos": pos, "id": mid, "xy_mm": [round(float(p[0]), 2), round(float(p[1]), 2)],
                             "z_mm": round(float(p[2]), 2), "joints": {str(j): round(v, 2) for j, v in q.items()}})
        print(f"tray {pos} marker {mid}: tip at ({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}) mm")
    elif step == "shape":
        # a photo of the tray at a new spot, NO touching: finish fits the map so that the tray's known shape
        # (20 mm squares, layout) comes out right there too. arm_server.py running, hands off.
        from arm_client import ArmClient
        pos = int(args[0])
        if any(t["pos"] == pos for t in s["touches"]):
            raise SystemExit(f"tray position {pos} has touches: use `photo {pos}` for it, or pick a new number")
        calib = load_calib()
        _, q = locate_tray(ArmClient(), calib, a.camera, confirm=lambda *x: None)
        dj1 = round(q[1] - calib["scan_pose"][1], 1)
        cap = open_camera(a.camera)
        try:
            frame = grab(cap, settle=10)
        finally:
            cap.release()
        corners = {i: c for i, c in detect_markers(frame) if i in MARKERS}
        cv2.imwrite(str(HERE / f"tray_scan_{pos}.jpg"), frame)
        print(f"tray position {pos} (no touches): base turned {dj1:+.1f} deg, markers {sorted(corners)}")
        if len(corners) < 2:
            raise SystemExit("NOT SAVED: need 2+ markers")
        s["photos"] = [ph for ph in s["photos"] if ph["pos"] != pos] + [
            {"pos": pos, "dj1": dj1, "corners": {str(i): np.round(c, 2).tolist() for i, c in corners.items()}}]
    elif step == "photo":
        from arm_client import ArmClient
        from vision import BASE_PIVOT_MM, BASE_SIGN
        pos = int(args[0])
        if "scan_pose" not in s:
            raise SystemExit("save the scan pose first: markers.py calibrate scan-pose")
        pts = [np.asarray(t["xy_mm"]) for t in s["touches"] if t["pos"] == pos]
        if len(args) > 1:
            dj1 = float(args[1])
        else:  # turn the base from the scan pose's heading towards this tray
            ax, ay = np.mean(pts, axis=0) - BASE_PIVOT_MM
            p_scan, _ = M.fk_mm({int(j): v for j, v in s["scan_pose"].items()})
            sx, sy = p_scan[:2] - BASE_PIVOT_MM
            dj1 = round(BASE_SIGN * math.degrees(math.atan2(ay, ax) - math.atan2(sy, sx)), 1)
        q = {int(j): v for j, v in s["scan_pose"].items()}
        q[1] += dj1
        print(f"scan pose with the base turned {dj1:+.1f} deg (J1 {q[1]:.1f}), at {MOVE_SPEED:g} deg/s")
        ArmClient()._send({"op": "move", "joints": {str(j): v for j, v in q.items()}, "speed": MOVE_SPEED})
        cap = open_camera(a.camera)
        try:
            time.sleep(1.0)  # still camera after the move
            frame = grab(cap, settle=10)
        finally:
            cap.release()
        corners = {i: c for i, c in detect_markers(frame) if i in MARKERS}
        cv2.imwrite(str(HERE / f"tray_scan_{pos}.jpg"), frame)
        print(f"markers seen {sorted(corners)} (photo in tray_scan_{pos}.jpg)")
        if len(corners) < 2:
            raise SystemExit("NOT SAVED: need 2+ markers; try another turn: markers.py calibrate photo POS DJ1")
        s["photos"] = [ph for ph in s["photos"] if ph["pos"] != pos] + [
            {"pos": pos, "dj1": dj1, "corners": {str(i): np.round(c, 2).tolist() for i, c in corners.items()}}]
    elif step == "finish":
        photos = [{"pos": ph["pos"], "dj1": ph["dj1"], "corners": {int(i): np.array(c) for i, c in ph["corners"].items()}}
                  for ph in s["photos"]]
        used = {ph["pos"] for ph in photos}
        touches = [t for t in s["touches"] if t["pos"] in used]
        calib, rep = calibrate_from(touches, photos)
        print("marker centre through the map vs. where the tip touched it:")
        for k, e in rep["points"].items():
            print(f"  {k}: {e:.1f} mm")
        print(f"RMS {rep['homography_rms_mm']:.1f} mm, worst {rep['homography_max_mm']:.1f} mm "
              f"(all corners: RMS {rep['corner_rms_mm']:.1f} mm)")
        if rep["tray_rigid_rms_mm"] is not None:
            print(f"tray layout across positions: RMS {rep['tray_rigid_rms_mm']:.1f} mm (touching / tray rigidity)")
        print(f"table height {calib['table_z_mm']:.1f} mm, spread {rep['table_z_spread_mm']:.1f} mm")
        save_calib({
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "camera": a.camera, "marker_mm": MARKER_MM,
            "markers": dict(MARKERS), "scan_pose": {int(j): v for j, v in s["scan_pose"].items()},
            "homography": np.asarray(calib["homography"]).tolist(),
            "layout_mm": {i: [round(float(v), 2) for v in xy] for i, xy in calib["layout_mm"].items()},
            "table_z_mm": round(calib["table_z_mm"], 2),
            "error_mm": {k: (round(v, 2) if v is not None else None) for k, v in rep.items() if k != "points"},
            "touches": touches, "photos": [{**ph, "corners": {i: np.round(c, 2).tolist() for i, c in
                                                                ph["corners"].items()}} for ph in photos],
        })
        print(f"saved the tray calibration to {CALIB_FILE.name}")
        return
    else:
        raise SystemExit(f"unknown step {step!r}: scan-pose, touch, photo, finish, reset")
    SESSION_FILE.write_text(json.dumps(s, indent=1))


APPROACH = 0  # column of the tool rotation along the gripper: in this URDF the tip is 166 mm along the tool x
              # axis from the wrist (checked on the taught grasp poses), not z as kinematics.py's docstring says
TILTS_DEG = (0, 10, 20, 30, 40, 50, 60)  # visit tries pointing straight down first; the arm can't reach that at carry
                                 # height (166 mm gripper, joint limits), so it then tilts the tip outward


def aim(R, direction) -> np.ndarray:
    """The orientation nearest to R (smallest turn) whose approach axis points along `direction`."""
    R = np.asarray(R, dtype=float)
    a, b = R[:, APPROACH], np.asarray(direction, dtype=float) / np.linalg.norm(direction)
    v, c = np.cross(a, b), float(a @ b)
    if np.linalg.norm(v) < 1e-9:
        return R if c > 0 else np.diag([-1.0, 1.0, -1.0]) @ R  # opposite: half turn about y
    V = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return (np.eye(3) + V + V @ V / (1 + c)) @ R


def point_down(R) -> np.ndarray:
    """The orientation nearest to R whose approach axis (gripper direction, wrist -> tip) points straight down."""
    return aim(R, [0.0, 0.0, -1.0])


def down_dir(xy, tilt_deg: float) -> np.ndarray:
    """Straight down, tilted by tilt_deg outward: the tip leans away from the base, towards (x, y)."""
    phi, t = math.atan2(xy[1], xy[0]), math.radians(tilt_deg)
    return np.array([math.sin(t) * math.cos(phi), math.sin(t) * math.sin(phi), -math.cos(t)])


def yaw(deg: float) -> np.ndarray:
    R = np.eye(3)
    R[:2, :2] = rot(deg)
    return R


def plan_visit(q_scan, containers: dict[int, np.ndarray], table_z: float, workspace=None,
               tilts=TILTS_DEG, hover_mm: float = HOVER_MM) -> tuple[list[tuple[int, list]], float]:
    """([(container, [Path, ...])], tilt used) from the scan pose, with the smallest tilt from `tilts` that plans.
    First container: straight up/down to carry height, then a straight line above its marker during which the
    gripper turns to point down (spread over the long travel so the wrist doesn't flip), then lower to HOVER_MM
    above the marker. Each next container: lift back to carry height, travel, lower. The gripper's heading turns
    with the base so the wrist doesn't wind up."""
    err = None
    for tilt in tilts:
        try:
            return _plan_visit(q_scan, containers, table_z, workspace, tilt, hover_mm), tilt
        except M.PlanError as e:
            err = e
    raise M.PlanError(f"no tilt in {tilts} works; last: {err}")


def _plan_visit(q_scan, containers, table_z, workspace, tilt, hover_mm=HOVER_MM):
    hover, carry = table_z + hover_mm, table_z + hover_mm + CARRY_MM
    plans, q = [], dict(q_scan)
    p, R = M.fk_mm(q)
    base0 = math.degrees(math.atan2(p[1], p[0]))
    for n, (cont, xy) in enumerate(sorted(containers.items())):
        p, _ = M.fk_mm(q)
        # turn with the base, then aim the tip down (leaning outward by `tilt`) over this container
        R_here = aim(yaw(math.degrees(math.atan2(xy[1], xy[0])) - base0) @ R, down_dir(xy, tilt))
        if n == 0:
            moves = [M.plan_line(q, [p[0], p[1], carry], None, speed_mm_s=M.DEFAULT_LIFT_SPEED,
                                 label="to carry height", workspace_mm=workspace)]
            moves.append(M.plan_line(moves[-1].final_joints, [xy[0], xy[1], carry], R_here,
                                     speed_mm_s=M.DEFAULT_TRAVEL_SPEED, workspace_mm=workspace,
                                     label=f"travel above container {cont}, turning the gripper to point down"))
        else:
            moves = [M.plan_lift(q, carry - p[2], workspace_mm=workspace)]
            moves.append(M.plan_line(moves[-1].final_joints, [xy[0], xy[1], carry], R_here,
                                     speed_mm_s=M.DEFAULT_TRAVEL_SPEED, workspace_mm=workspace,
                                     label=f"travel above container {cont}"))
        p, _ = M.fk_mm(moves[-1].final_joints)
        moves.append(M.plan_lower(moves[-1].final_joints, p[2] - hover, workspace_mm=workspace))
        q = moves[-1].final_joints
        plans.append((cont, moves))
    return plans


def settle_on(client, target_mm, log=print) -> tuple[float, np.ndarray]:
    """Close the loop on the encoders: the arm sags under load, by an amount that depends on the pose, the load and
    the motors at that moment (J3 ~2.5 deg, J4 ~2 deg at the tray, from the Tiger trace), so the tip stops short
    and low of the commanded point. Read where the tip really is, move the command by the difference, repeat.
    Returns (final measured error in mm, offset = commanded tip - target: the compensation that works HERE)."""
    target = np.asarray(target_mm, dtype=float)
    err = float("inf")
    for k in range(CORRECT_ITERS + 1):
        time.sleep(0.5)  # let the arm settle before reading it
        st = client.status()
        p_meas, _ = M.fk_mm({j: float(st["state"][f"J{j}"]) for j in range(1, 7)})
        miss = target - p_meas
        err = float(np.linalg.norm(miss))
        log(f"    measured tip {err:.1f} mm from the target ({miss[0]:+.1f}, {miss[1]:+.1f}, {miss[2]:+.1f})")
        if err <= CORRECT_TOL_MM or k == CORRECT_ITERS:
            break
        q_cmd = client.current_joints()
        p_cmd, _ = M.fk_mm(q_cmd)
        goal = p_cmd + miss
        if np.linalg.norm(goal - target) > CORRECT_MAX_MM:
            log(f"    not correcting further: the command would be {np.linalg.norm(goal - target):.0f} mm from the "
                f"target (> {CORRECT_MAX_MM:g})")
            break
        client.run(M.plan_line(q_cmd, goal, None, speed_mm_s=10.0, label=f"correct {err:.0f} mm"))
    p_cmd, _ = M.fk_mm(client.current_joints())
    return err, p_cmd - target


def run_container(client, n: int, plan: list, workspace, learned, confirm=input, log=print) -> np.ndarray:
    """Drive one planned container visit, measuring and correcting the sag after EVERY move that ends over the
    marker, wherever the tray is:
        to carry height (first container: the planned move from the scan pose; later: a lift)
        travel above the marker  -> measure + correct   (the tip is over the marker before it descends)
        lower to the hover point -> measure + correct   (lands HOVER_MM above the marker centre)
    Each move is aimed at target + `learned`, the compensation measured at the previous stop, so it starts close;
    the measurement then trims what is left at this place and moment. Returns the new learned offset."""
    carry_t, R_t = M.fk_mm(plan[-2].final_joints)   # end of the travel: above the marker at carry height
    hover_t, _ = M.fk_mm(plan[-1].final_joints)     # the hover point
    learned = np.asarray(learned, dtype=float)
    # The workspace box guards where the tip really goes: the plan already checked these moves against it. The
    # compensated commands below aim past the targets on purpose (by the sag), so they are not checked again.
    workspace = None

    def go(path):
        log("  " + path.summary())
        confirm("  Enter to move ")
        log("  " + str(client.run(path).get("result")))

    if n == 0:
        go(plan[0])
    else:
        q = client.current_joints()
        p, _ = M.fk_mm(q)
        go(M.plan_line(q, [p[0], p[1], carry_t[2] + learned[2]], None, speed_mm_s=M.DEFAULT_LIFT_SPEED,
                       label="lift to carry height", workspace_mm=workspace))
    go(M.plan_line(client.current_joints(), carry_t + learned, R_t, speed_mm_s=M.DEFAULT_TRAVEL_SPEED,
                   label=plan[-2].label, workspace_mm=workspace))
    _, learned = settle_on(client, carry_t, log)
    go(M.plan_line(client.current_joints(), hover_t + learned, None, speed_mm_s=M.DEFAULT_LOWER_SPEED,
                   label="lower over the marker", workspace_mm=workspace))
    _, learned = settle_on(client, hover_t, log)
    return learned


def locate_tray(client, calib: dict, camera: int, confirm=input) -> tuple[dict, dict]:
    """arm_server.py running: go to the scan pose, turn the base until the tray is seen, lock in there (LOCK_PHOTOS
    photos averaged). Returns (tray pose, the scan joints it was seen from).
    With the wrist camera calibrated (handeye.yaml, from `handeye.py orbit`) and tray_look.yaml, the tray is
    found from there with that calibration instead: it works from any arm pose, so it does not need the markers
    where the scan-pose homography was taught (they moved onto the container tops, 2026-10-25)."""
    client.current_joints()  # refuses now if arm_server.py isn't running
    look_file = HERE / "tray_look.yaml"
    look = yaml.safe_load(look_file.read_text())["joints"] if look_file.exists() else None
    if look and (HERE / "handeye.yaml").exists():
        return locate_tray_handeye(client, look, camera, confirm)
    q_scan = calib["scan_pose"]
    pose = None
    for dj1 in SWEEP_DEG:     # turn only the base until the tray is in view, then lock in at that angle
        q_look = {**q_scan, 1: q_scan[1] + dj1}
        confirm(f"Enter: joint move to the scan pose{f' with the base turned {dj1:+g} deg' if dj1 else ''} "
              f"at {MOVE_SPEED:g} deg/s (Ctrl+C stops) ")
        client._send({"op": "move", "joints": {str(j): v for j, v in q_look.items()}, "speed": MOVE_SPEED})
        looks = []
        cap = open_camera(camera)
        try:
            for n in range(LOCK_PHOTOS):  # first detection, then a second photo to confirm it
                time.sleep(0.5 if n else 0.0)
                try:
                    looks.append(find_tray(grab(cap, settle=10), calib, dj1))
                except ValueError as e:
                    print(f"  base {dj1:+g} deg, photo {n + 1}: {e}")
        finally:
            cap.release()
        if looks:
            pose = lock_in(looks)
            q_scan = q_look
            print(f"  base {dj1:+g} deg: locked in on the tray from {len(looks)} detection(s), "
                  f"markers {pose['seen']}" + (f", photos agree within {pose['spread_mm']:.1f} mm"
                                               if len(looks) > 1 else ""))
            break
    if pose is None:
        raise SystemExit("tray not found at any base angle")
    return pose, q_scan


def locate_tray_handeye(client, q_look: dict, camera: int, confirm=input) -> tuple[dict, dict]:
    """locate_tray with the calibrated wrist camera: from the tray_look.yaml pose (base turned if needed), each photo
    locates every marker in 3-D from the measured joints, a rigid fit of the marker layout gives the tray pose."""
    import handeye as H
    he = H.load()
    q_look = {int(j): float(v) for j, v in q_look.items()}
    for dj1 in SWEEP_DEG:
        q = {**q_look, 1: q_look[1] + dj1}
        confirm(f"Enter: joint move to the tray look pose{f' with the base turned {dj1:+g} deg' if dj1 else ''} ")
        client._send({"op": "move", "joints": {str(j): v for j, v in q.items()}, "speed": MOVE_SPEED})
        looks = []
        cap = open_camera(camera)
        try:
            for n in range(LOCK_PHOTOS + 1):
                time.sleep(0.5)
                frame = grab(cap, settle=10)
                st = client.status() or {}
                q_meas = {j: float(st["state"][f"J{j}"]) for j in range(1, 7)}
                try:
                    looks.append(H.tray_from(he, H.locate(he, q_meas, frame)))
                except ValueError as e:
                    print(f"  base {dj1:+g} deg, photo {n + 1}: {e}")
        finally:
            cap.release()
        if looks:
            pose = lock_in(looks)
            pose["rms_mm"] = float(np.mean([lk["rms_mm"] for lk in looks]))
            print(f"  base {dj1:+g} deg: tray at ({pose['xy'][0]:.1f}, {pose['xy'][1]:.1f}) mm, heading "
                  f"{pose['heading_deg']:.1f} deg, markers {pose['seen']}, fit {pose['rms_mm']:.1f} mm, photos agree "
                  f"within {pose['spread_mm']:.1f} mm (wrist camera calibration)")
            return pose, q
    raise SystemExit("tray not found at any base angle from the tray look pose")


def cmd_visit(a) -> None:
    calib = load_calib()
    bench = yaml.safe_load(BENCH_FILE.read_text()) if BENCH_FILE.exists() else {}
    ws = bench.get("workspace_mm")
    workspace = (ws["min"], ws["max"]) if ws else None
    q_scan = calib["scan_pose"]

    client = None
    if a.run:
        from arm_client import ArmClient
        client = ArmClient()
        pose, q_scan = locate_tray(client, calib, a.camera)
    else:
        if not a.image:
            raise SystemExit("without --run, give a scan-pose photo: --image tray_scan_1.jpg [--dj1 deg]")
        pose = find_tray(cv2.imread(a.image), calib, a.dj1)
        q_scan = {**q_scan, 1: q_scan[1] + a.dj1}
    print(f"tray at ({pose['xy'][0]:.1f}, {pose['xy'][1]:.1f}) mm, heading {pose['heading_deg']:.1f} deg, "
          f"fit {pose['rms_mm']:.1f} mm from markers {pose['seen']}")
    for k, xy in pose["containers"].items():
        print(f"  container {k}: ({xy[0]:.1f}, {xy[1]:.1f}) mm")

    try:
        plans, tilt = plan_visit(q_scan, pose["containers"], calib["table_z_mm"], workspace, hover_mm=a.hover)
    except M.PlanError as e:
        raise SystemExit(f"NOT MOVING: {e}") from None
    print("gripper pointing straight down" if tilt == 0 else
          f"gripper pointing down, tip leaning {tilt:g} deg outward (straight down is out of reach)")
    learned = np.zeros(3)  # sag compensation that worked at the last stop (commanded - target, mm)
    for n, (cont, moves) in enumerate(plans):
        print(f"\ncontainer {cont}")
        if not client:
            for p in moves:
                print("  " + p.summary())
            continue
        try:
            learned = run_container(client, n, moves, workspace, learned)
        except M.PlanError as e:
            raise SystemExit(f"STOPPED at container {cont} (arm holds where it is): {e}") from None
    print("\ndone" + (": arm_server is holding above the last container (python arm_cmd.py rest to rest)"
                      if client else " (print only: add --run to move)"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["view", "calibrate", "visit"])
    ap.add_argument("step", nargs="?", help="calibrate: scan-pose | touch POS ID | photo POS [DJ1] | shape POS | finish | reset")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--wait", type=float, default=60.0, help="calibrate touch: seconds to wait for a still tip")
    ap.add_argument("--camera", type=int, default=CAMERA, help="wrist camera index")
    ap.add_argument("--run", action="store_true", help="visit: move the arm (arm_server.py must be running)")
    ap.add_argument("--image", help="visit without --run: plan from this scan-pose photo")
    ap.add_argument("--hover", type=float, default=HOVER_MM, help="visit: stop this far above each marker centre, mm")
    ap.add_argument("--dj1", type=float, default=0.0, help="visit --image: the base was turned this much (deg)")
    a = ap.parse_args()
    {"view": cmd_view, "calibrate": cmd_calibrate, "visit": cmd_visit}[a.command](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nstopped (arm_server, if running, holds the arm where it is)")
