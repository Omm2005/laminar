"""Find the container tray with the wrist camera and drive the gripper above each container.

The tray: the 4 containers are stuck together and always move as one. Each has an ArUco marker (DICT_4X4_50,
ids 10-13 = containers 1-4, 40 mm) lying flat on the table beside it. Judges will move the tray, so nothing here
hardcodes where it is: one photo from the SCAN POSE (arm high, wrist camera seeing the whole tray) says where.

How, in plain words
    1. Calibrate once (`calibrate`, while you teleop). With the tray in one place, touch the centre of each marker
       with the gripper tip; forward kinematics of the follower's joints gives each marker's (x, y) in mm. Teleop up
       to the scan pose and take a photo. Slide the tray far away and do it all again: 8 touched points.
    2. From those: a homography (pixel -> table mm, valid for photos taken from the scan pose), the table height,
       and the tray LAYOUT: where each marker sits in the tray's own frame (origin = the markers' centroid,
       x axis = from marker 10 towards marker 13).
    3. Afterwards one photo from the scan pose finds the tray: every visible marker's centre goes through the
       homography, and a rigid fit (rotation + translation, no scaling) of the layout onto those points gives the
       tray's (x, y, heading). Two visible markers are enough, so one or two can be hidden.
    4. Each container's position = the tray pose applied to its marker's layout position, hidden ones included.

The scan pose must be the same for every photo (the homography is tied to it). `visit` goes there exactly through
arm_server.py; during `calibrate` the second scan photo is only accepted when every joint is within --scan-tol of
the first one.

Commands (degrees for joints, mm in the arm base frame: x forward, y left, z up)
    python markers.py view                  live wrist camera: marker ids + the fitted tray outline. Never moves
                                            the arm; runs alongside teleop.py.
    python markers.py calibrate             guided, while teleop.py runs; writes the `tray` section of
                                            vision_calib.yaml and prints the error in mm
    python markers.py visit [--run]         arm_server.py running: scan pose -> find the tray -> for each container
                                            lift, travel, lower to HOVER_MM above its marker, gripper pointing
                                            down; Enter before every move. Without --run: plan and print only.
Joints during teleop come from teleop_state.json, which teleop.py writes at 10 Hz.
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
from vision import detect_markers

HERE = Path(__file__).resolve().parent
CALIB_FILE = HERE / "vision_calib.yaml"     # shared with vision.py; this module owns the `tray` section
TELEOP_STATE = HERE / "teleop_state.json"   # follower joints written by teleop.py while it runs
STREAM_FILE = HERE / "wrist_latest.jpg"     # latest frame from wrist_stream.py, when it owns the camera
BENCH_FILE = HERE / "bench.yaml"

MARKERS = {10: 1, 11: 2, 12: 3, 13: 4}      # ArUco id -> container number
MARKER_MM = 40.0
HOVER_MM = 100.0       # visit: gripper tip ends this far above the marker
CARRY_MM = 60.0        # ... and travels between containers this much higher than that
CAMERA = 2             # wrist camera, DirectShow index (indices shift after re-plugs: --camera)
SCAN_TOL_DEG = 1.0     # calibrate: second scan photo must be this close to the first scan pose, every joint
STATE_MAX_AGE_S = 1.0  # teleop_state.json older than this = teleop.py is not running
MOVE_SPEED = 15.0      # deg/s for the joint move to the scan pose


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


def find_tray(frame, calib: dict) -> dict:
    """One scan-pose photo -> tray pose (see tray_pose), plus "px" = the pixel centre of each seen marker."""
    px = {i: marker_center(c) for i, c in detect_markers(frame) if i in MARKERS}
    if not px:
        raise ValueError("no tray markers in the picture")
    xy = dict(zip(px, px_to_mm(calib["homography"], list(px.values()))))
    pose = tray_pose(calib["layout_mm"], xy)
    pose["px"] = px
    return pose


def calibrate_from(touches: list[dict], photos: list[dict]) -> tuple[dict, dict]:
    """touches: [{"pos": 1|2, "id", "xy_mm", "z_mm"}], photos: [{"pos", "px": {id: (u, v)}}] -> (calib, report).
    Each tray position contributes 4 touched points and the pixel centres from its scan photo."""
    pairs_px, pairs_xy, labels = [], [], []
    for t in touches:
        ph = next(p for p in photos if p["pos"] == t["pos"])
        if t["id"] in ph["px"]:
            pairs_px.append(ph["px"][t["id"]])
            pairs_xy.append(t["xy_mm"])
            labels.append(f"tray {t['pos']} marker {t['id']}")
    H, err = fit_homography(pairs_px, pairs_xy)
    # layout from the first position; the second, fitted onto it, says how rigid the tray really is
    at = {p: {t["id"]: np.asarray(t["xy_mm"], dtype=float) for t in touches if t["pos"] == p} for p in (1, 2)}
    layout = tray_layout(at[1])
    rigid_rms = tray_pose(layout, at[2])["rms_mm"] if len(at.get(2, {})) >= 2 else None
    if rigid_rms is not None:  # average both positions, each brought into the tray frame
        ang, t, _ = rigid_fit([layout[i] for i in sorted(at[2])], [at[2][i] for i in sorted(at[2])])
        back = {i: rot(ang).T @ (at[2][i] - t) for i in at[2]}
        layout = {i: (layout[i] + back[i]) / 2 if i in back else layout[i] for i in layout}
    zs = [t["z_mm"] for t in touches]
    calib = {"homography": H, "layout_mm": layout, "table_z_mm": float(np.mean(zs))}
    report = {"points": dict(zip(labels, err)), "homography_rms_mm": float(np.sqrt(np.mean(err ** 2))),
              "homography_max_mm": float(err.max()), "tray_rigid_rms_mm": rigid_rms,
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
            pose = find_tray(frame, calib)
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


def _scan_photo(cap, label: str, ref_pose: dict[int, float] | None, tol: float) -> tuple[dict, dict]:
    """Live window until SPACE with all 4 markers seen (and, with ref_pose, every joint within tol).
    Returns (scan joints, {id: pixel centre})."""
    print(f"  {label}: teleop up to the scan pose so the camera sees the whole tray, then SPACE in the window "
          f"(q aborts)")
    while True:
        frame = grab(cap)
        img, _ = draw(frame, None)
        seen = {i: marker_center(c) for i, c in detect_markers(frame) if i in MARKERS}
        msg, ok = f"seen {sorted(seen)}", len(seen) == len(MARKERS)
        try:
            q = teleop_joints()
            if ref_pose:
                off = max(abs(q[j] - ref_pose[j]) for j in ref_pose)
                ok = ok and off <= tol
                msg += f"   off scan pose {off:.2f} deg (need <= {tol:g})"
        except RuntimeError as e:
            q, ok, msg = None, False, str(e)
        cv2.putText(img, msg, (8, 470), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0) if ok else (0, 0, 255), 2)
        cv2.imshow("markers calibrate", img)
        k = cv2.waitKey(1) & 0xFF
        if k in (ord("q"), 27):
            raise SystemExit("calibration aborted, nothing saved")
        if k == ord(" ") and ok:
            cv2.imwrite(str(HERE / f"tray_scan_{label[-1]}.jpg"), frame)
            return q, seen
        if k == ord(" "):
            print(f"    not yet: {msg}")


def cmd_calibrate(a) -> None:
    cap = open_camera(a.camera)
    touches, photos, scan_pose = [], [], None
    try:
        for pos in (1, 2):
            print(f"\nTray position {pos}" + (" (slide the tray far from position 1 first)" if pos == 2 else ""))
            for mid, cont in MARKERS.items():
                while True:
                    input(f"  teleop the gripper tip onto the centre of marker {mid} (container {cont}), Enter ")
                    try:
                        q = teleop_joints()
                        break
                    except RuntimeError as e:
                        print(f"    {e}")
                p, _ = M.fk_mm(q)
                touches.append({"pos": pos, "id": mid, "xy_mm": [float(p[0]), float(p[1])], "z_mm": float(p[2]),
                                "joints": q})
                print(f"    marker {mid}: ({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}) mm")
            q, px = _scan_photo(cap, f"scan photo {pos}", scan_pose, a.scan_tol)
            scan_pose = scan_pose or q
            photos.append({"pos": pos, "px": px})
    finally:
        cap.release()
        cv2.destroyAllWindows()

    calib, rep = calibrate_from(touches, photos)
    print("\nerror per point (touched vs. homography of the photo), mm:")
    for k, e in rep["points"].items():
        print(f"  {k}: {e:.1f}")
    print(f"homography: RMS {rep['homography_rms_mm']:.1f} mm, worst {rep['homography_max_mm']:.1f} mm")
    if rep["tray_rigid_rms_mm"] is not None:
        print(f"tray layout, position 2 fitted onto position 1: RMS {rep['tray_rigid_rms_mm']:.1f} mm "
              "(large = a touch was off, or the tray isn't rigid)")
    print(f"table height {calib['table_z_mm']:.1f} mm, spread {rep['table_z_spread_mm']:.1f} mm over the touches")
    save_calib({
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "camera": a.camera, "marker_mm": MARKER_MM,
        "markers": {i: c for i, c in MARKERS.items()},
        "scan_pose": {j: round(scan_pose[j], 2) for j in range(1, 7)},
        "homography": np.asarray(calib["homography"]).tolist(),
        "layout_mm": {i: [round(float(v), 2) for v in xy] for i, xy in calib["layout_mm"].items()},
        "table_z_mm": round(calib["table_z_mm"], 2),
        "error_mm": {k: (round(v, 2) if v is not None else None) for k, v in rep.items() if k != "points"},
        "touches": [{k: v for k, v in t.items() if k != "joints"} | {"joints": {j: round(x, 2) for j, x in
                                                                                 t["joints"].items()}}
                    for t in touches],
    })
    print(f"saved the tray calibration to {CALIB_FILE.name}")


APPROACH = 0  # column of the tool rotation along the gripper: in this URDF the tip is 166 mm along the tool x
              # axis from the wrist (checked on the taught grasp poses), not z as kinematics.py's docstring says
TILTS_DEG = (0, 10, 20, 30, 40)  # visit tries pointing straight down first; the arm can't reach that at carry
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
               tilts=TILTS_DEG) -> tuple[list[tuple[int, list]], float]:
    """([(container, [Path, ...])], tilt used) from the scan pose, with the smallest tilt from `tilts` that plans.
    First container: straight up/down to carry height, then a straight line above its marker during which the
    gripper turns to point down (spread over the long travel so the wrist doesn't flip), then lower to HOVER_MM
    above the marker. Each next container: lift back to carry height, travel, lower. The gripper's heading turns
    with the base so the wrist doesn't wind up."""
    err = None
    for tilt in tilts:
        try:
            return _plan_visit(q_scan, containers, table_z, workspace, tilt), tilt
        except M.PlanError as e:
            err = e
    raise M.PlanError(f"no tilt in {tilts} works; last: {err}")


def _plan_visit(q_scan, containers, table_z, workspace, tilt):
    hover, carry = table_z + HOVER_MM, table_z + HOVER_MM + CARRY_MM
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
        client.current_joints()  # refuses now if arm_server.py isn't running
        input(f"Enter: joint move to the scan pose at {MOVE_SPEED:g} deg/s (Ctrl+C stops) ")
        client._send({"op": "move", "joints": {str(j): v for j, v in q_scan.items()}, "speed": MOVE_SPEED})
        cap = open_camera(a.camera)
        try:
            frame = grab(cap, settle=10)
        finally:
            cap.release()
    else:
        if not a.image:
            raise SystemExit("without --run, give a scan-pose photo: --image tray_scan_1.jpg")
        frame = cv2.imread(a.image)
    pose = find_tray(frame, calib)
    print(f"tray at ({pose['xy'][0]:.1f}, {pose['xy'][1]:.1f}) mm, heading {pose['heading_deg']:.1f} deg, "
          f"fit {pose['rms_mm']:.1f} mm from markers {pose['seen']}")
    for k, xy in pose["containers"].items():
        print(f"  container {k}: ({xy[0]:.1f}, {xy[1]:.1f}) mm")

    try:
        plans, tilt = plan_visit(q_scan, pose["containers"], calib["table_z_mm"], workspace)
    except M.PlanError as e:
        raise SystemExit(f"NOT MOVING: {e}") from None
    print("gripper pointing straight down" if tilt == 0 else
          f"gripper pointing down, tip leaning {tilt:g} deg outward (straight down is out of reach)")
    for cont, moves in plans:
        print(f"\ncontainer {cont}")
        for p in moves:
            print("  " + p.summary())
            if client:
                input("  Enter to move ")
                print("  " + str(client.run(p).get("result")))
    print("\ndone" + (": arm_server is holding above the last container (python arm_cmd.py rest to rest)"
                      if client else " (print only: add --run to move)"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["view", "calibrate", "visit"])
    ap.add_argument("--camera", type=int, default=CAMERA, help="wrist camera index")
    ap.add_argument("--scan-tol", type=float, default=SCAN_TOL_DEG,
                    help="calibrate: max joint difference between the two scan photos, deg")
    ap.add_argument("--run", action="store_true", help="visit: move the arm (arm_server.py must be running)")
    ap.add_argument("--image", help="visit without --run: plan from this scan-pose photo")
    a = ap.parse_args()
    {"view": cmd_view, "calibrate": cmd_calibrate, "visit": cmd_visit}[a.command](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nstopped (arm_server, if running, holds the arm where it is)")
