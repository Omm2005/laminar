"""Wrist-camera calibration from the arm itself, and precise 3-D marker positions anywhere the tray is.

The old tray finder (markers.py) maps pixels to the table with one homography fitted at the scan pose. It is only
right near where it was calibrated: a tray moved 50 mm put the pour a channel off. This replaces it with a real
camera model:

    python handeye.py collect --run     the arm moves the wrist camera through ~30 viewpoints over the tray (above a
                                        safe height, nothing touched) and photographs the markers each time
    python handeye.py orbit --run       better: find the markers, centre them in the view, then circle around them
                                        (7 yaws x 3 heights x 2 tilts), re-centring before every photo
    python handeye.py solve             one least-squares fit of everything at once (bundle adjustment):
                                          lens: focal length, centre, radial distortion
                                          hand-eye: where the camera sits on the gripper (tool frame)
                                          the tray: its pose in the base frame and the 4 markers' layout on it
                                        -> handeye.yaml, with the reprojection error and a cross-check: the same
                                           marker located from every viewpoint separately must agree (spread, mm)
    python handeye.py locate [--run]    from the current pose: each visible marker's centre (mm, base frame)

Locating a marker afterwards: solvePnP on its 4 corners (it is a 20 mm square) gives it in the camera frame; the
arm's MEASURED joints (encoders, so the sag is in) and the hand-eye transform put it in the base frame. Nothing
depends on where the tray was during calibration.

Units: mm and degrees; base frame x forward, y left, z up.
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

import markers as T
import motion as M

HERE = Path(__file__).resolve().parent
CALIB = HERE / "handeye.yaml"
MARKER_MM = T.MARKER_MM
IDS = sorted(T.MARKERS)
SQUARE = np.array([[-1, 1, 0], [1, 1, 0], [1, -1, 0], [-1, -1, 0]], float) * MARKER_MM / 2   # ArUco corner order
MIN_TOOL_Z_MM = 150.0      # collect: never bring the gripper tip lower than this above the base (table ~64 mm)
MOVE_SPEED = 12.0          # deg/s between viewpoints
SETTLE_S = 1.2             # after a move, before the photo (the arm stops swaying, a fresh frame arrives)


# ------------------------------------------------------------------------------------------------- geometry

def rt_to_T(rvec, tvec) -> np.ndarray:
    Tm = np.eye(4)
    Tm[:3, :3] = cv2.Rodrigues(np.asarray(rvec, float).reshape(3))[0]
    Tm[:3, 3] = np.asarray(tvec, float).reshape(3)
    return Tm


def T_to_rt(Tm) -> tuple[np.ndarray, np.ndarray]:
    return cv2.Rodrigues(Tm[:3, :3])[0].reshape(3), Tm[:3, 3].copy()


def tool_T(q) -> np.ndarray:
    """Base -> tool (gripper tip) for joints q, mm."""
    p, R = M.fk_mm({j: float(q[j]) for j in range(1, 7)})
    Tm = np.eye(4)
    Tm[:3, :3], Tm[:3, 3] = R, p
    return Tm


def K_of(f, cx, cy) -> np.ndarray:
    return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])


def project(P_cam: np.ndarray, f, cx, cy, k1, k2) -> np.ndarray:
    """Pinhole + 2-term radial distortion. P_cam (n, 3) -> pixels (n, 2)."""
    x, y = P_cam[:, 0] / P_cam[:, 2], P_cam[:, 1] / P_cam[:, 2]
    r2 = x * x + y * y
    d = 1 + k1 * r2 + k2 * r2 * r2
    return np.stack([f * x * d + cx, f * y * d + cy], axis=1)


def marker_T(u, v, th) -> np.ndarray:
    """Tray frame -> marker frame (markers lie flat on the tray)."""
    c, s = math.cos(th), math.sin(th)
    Tm = np.eye(4)
    Tm[:2, :2] = [[c, -s], [s, c]]
    Tm[:2, 3] = [u, v]
    return Tm


# ------------------------------------------------------------------------------------------------- collect

def viewpoints(q_scan: dict) -> list[dict]:
    """Joint poses around the scan pose: base turns, reach in/out, wrist tilts. Unsafe ones are dropped later."""
    out = []
    for d1 in (-14, -7, 0, 7, 14):
        for d23 in ((0, 0), (-6, 4), (5, -5)):
            for d456 in ((0, 0, 0), (8, -6, 15), (-8, 8, -15)):
                q = dict(q_scan)
                q[1] += d1
                q[2] += d23[0]
                q[3] += d23[1]
                q[4] += d456[0]
                q[5] += d456[1]
                q[6] += d456[2]
                out.append(q)
    return out


def safe(q) -> bool:
    for j, (lo, hi) in M.JOINT_LIMITS_DEG.items():
        if not lo <= q[j] <= hi:
            return False
    return tool_T(q)[2, 3] >= MIN_TOOL_Z_MM


def fresh_frame(after: float, timeout: float = 3.0):
    """The wrist stream's newest frame taken after `after` (wrist_stream.py must be running)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if T.STREAM_FILE.stat().st_mtime > after:
                f = cv2.imread(str(T.STREAM_FILE))
                if f is not None:
                    return f
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("no fresh wrist frame: is wrist_stream.py running?")


# ------------------------------------------------------------------------------------------------- orbit scan

ORBIT_YAW_DEG = (-30, -20, -10, 0, 10, 20, 30)   # around the vertical axis through the tray
ORBIT_DZ_MM = (-30, 0, 30)                         # camera height steps
ORBIT_TILT_DEG = (-8, 8)                           # tip the view towards / away from the tray
CENTER_TOL_PX = 25.0                               # markers' centroid this close to the image centre is enough
CENTER_ITERS = 4
CENTER_STEP_MM = 10.0                              # test moves for the centring Jacobian
CENTER_MAX_MM = 30.0                               # one centring move at most


def _rot_z_about(Tm: np.ndarray, P: np.ndarray, deg: float) -> np.ndarray:
    a = math.radians(deg)
    R = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1.0]])
    out = Tm.copy()
    out[:3, :3] = R @ Tm[:3, :3]
    out[:3, 3] = R @ (Tm[:3, 3] - P) + P
    return out


def _tilt_about(Tm: np.ndarray, P: np.ndarray, deg: float) -> np.ndarray:
    """Rotate the pose about the horizontal axis through P perpendicular to the line from the base to P."""
    r = np.array([P[0], P[1], 0.0])
    axis = np.cross([0, 0, 1.0], r / np.linalg.norm(r))
    R = cv2.Rodrigues(axis * math.radians(deg))[0]
    out = Tm.copy()
    out[:3, :3] = R @ Tm[:3, :3]
    out[:3, 3] = R @ (Tm[:3, 3] - P) + P
    return out


def _snap(c):
    """(frame, measured joints, {id: corners}) after the arm has settled."""
    t_done = time.time()
    time.sleep(SETTLE_S)
    frame = fresh_frame(t_done + SETTLE_S - 0.3)
    st = c.status() or {}
    q_meas = {j: float(st["state"][f"J{j}"]) for j in range(1, 7)}
    return frame, q_meas, {i: cc for i, cc in T.detect_markers(frame) if i in IDS}


def _centroid(dets) -> np.ndarray:
    return np.mean([T.marker_center(c) for c in dets.values()], axis=0)


def _shift(c, d_xy, label) -> None:
    q = c.current_joints()
    p, _ = M.fk_mm(q)
    c.run(M.plan_line(q, p + np.array([d_xy[0], d_xy[1], 0.0]), None, speed_mm_s=20.0, label=label))


def center_on_markers(c, J: np.ndarray | None) -> tuple[np.ndarray | None, dict]:
    """Slide the gripper sideways until the markers' centroid is near the image centre. Learns the Jacobian (px per
    mm of sideways move) with two test moves the first time. Returns (Jacobian, last detections)."""
    centre = np.array([320.0, 240.0])
    _, _, dets = _snap(c)
    if len(dets) < 2:
        return J, dets
    if J is None:
        base = _centroid(dets)
        cols = []
        for axis in (0, 1):
            d = np.zeros(2)
            d[axis] = CENTER_STEP_MM
            _shift(c, d, "centring test")
            _, _, dd = _snap(c)
            _shift(c, -d, "centring test back")
            if len(dd) < 2:
                return None, dets
            cols.append((_centroid(dd) - base) / CENTER_STEP_MM)
        J = np.column_stack(cols)
        if abs(np.linalg.det(J)) < 1e-3:
            return None, dets
    for _ in range(CENTER_ITERS):
        err = centre - _centroid(dets)
        if np.linalg.norm(err) <= CENTER_TOL_PX:
            break
        d = np.linalg.solve(J, err)
        n = float(np.linalg.norm(d))
        if n > CENTER_MAX_MM:
            d *= CENTER_MAX_MM / n
        q = c.current_joints()
        p, _ = M.fk_mm(q)
        if p[2] < MIN_TOOL_Z_MM:
            break
        try:
            _shift(c, d, "centre on markers")
        except M.PlanError as e:  # planned before any motion: the arm has not moved, photograph from here
            print(f"  centring stopped short: {e}")
            break
        _, _, dets = _snap(c)
        if len(dets) < 2:
            break
    return J, dets


def cmd_orbit(a) -> None:
    """Find the markers from the scan pose, centre them in the view, then circle around them (yaw about the vertical
    through the tray, 3 heights, 2 tilts), re-centring before every photo, so every view sees the markers."""
    from arm_client import ArmClient
    if not a.run:
        raise SystemExit("moves the arm around the tray: add --run (arm_server.py and wrist_stream.py running)")
    c = ArmClient()
    c.current_joints()
    calib = T.load_calib()
    q_scan = {int(k): float(v) for k, v in calib["scan_pose"].items()}
    out = HERE / "logs" / time.strftime("handeye_orbit_%Y%m%d_%H%M%S")
    out.mkdir(parents=True)
    # start where the markers are in view: the scan pose, else the best view of the latest sweep (all 4 markers,
    # closest to the image centre)
    starts = [q_scan]
    for folder in sorted((HERE / "logs").glob("handeye_2*"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]:
        views = json.loads((folder / "views.json").read_text())
        good = [v for v in views if len(v["corners"]) >= 3]
        good.sort(key=lambda v: np.linalg.norm(np.mean([T.marker_center(np.asarray(x)) for x in v["corners"].values()],
                                                       axis=0) - [320, 240]))
        starts += [{int(k): float(x) for k, x in v["q_cmd"].items()} for v in good[:3]]
    dets = {}
    for q0 in starts:
        c._send({"op": "move", "joints": {str(j): v for j, v in q0.items()}, "speed": MOVE_SPEED})
        frame, _, dets = _snap(c)
        if len(dets) >= 3:
            break
    if len(dets) < 2:
        raise SystemExit(f"markers not in view from any start pose (saw {sorted(dets)}): put the tray in view")
    # the orbit's centre: the tray where it was last found (container_offsets.yaml), at about the markers' height
    import deliver as D
    tl = yaml.safe_load(D.OFFSETS_FILE.read_text())["tray_at_learn"]["xy_mm"]
    P = np.array([tl[0], tl[1], float(calib.get("table_z_mm", 64.0)) + 60.0])
    print(f"tray near ({P[0]:.0f}, {P[1]:.0f}) mm; centring the markers in the view")
    J, dets = center_on_markers(c, None)
    if J is None:
        raise SystemExit("could not learn the centring Jacobian (markers lost in the test moves)")
    T0 = tool_T(c.current_joints())
    targets, k = [], 0
    for t in ORBIT_TILT_DEG:                 # zig-zag: neighbouring views are always close (short, safe moves)
        for dz in ORBIT_DZ_MM:
            yaws = ORBIT_YAW_DEG if k % 2 == 0 else tuple(reversed(ORBIT_YAW_DEG))
            targets += [(y, dz, t) for y in yaws]
            k += 1
    print(f"{len(targets)} orbit views planned; photos in {out.relative_to(HERE)}")
    data, n = [], 0
    try:
        for yaw, dz, tilt in targets:
            Tt = _rot_z_about(_tilt_about(T0, P, tilt), P, yaw)
            Tt[2, 3] += dz
            if Tt[2, 3] < MIN_TOOL_Z_MM:
                continue
            q0 = c.current_joints()
            seed = {**q0, 1: q0[1] + yaw}
            qn, pe, ae = M.ik(seed, Tt[:3, 3], Tt[:3, :3])
            qn = dict(qn)
            if pe > 2.0 or ae > 2.0 or not safe(qn):
                print(f"  skip yaw {yaw:+d} dz {dz:+d} tilt {tilt:+d}: out of reach")
                continue
            c._send({"op": "move", "joints": {str(j): v for j, v in qn.items()}, "speed": MOVE_SPEED})
            J, dets = center_on_markers(c, J)
            frame, q_meas, dets = _snap(c)
            cv2.imwrite(str(out / f"{n:02d}.jpg"), frame)
            data.append({"n": n, "q_cmd": c.current_joints(), "q_meas": q_meas,
                         "corners": {i: cc.tolist() for i, cc in dets.items()}})
            (out / "views.json").write_text(json.dumps(data))
            print(f"  {n + 1:2d}: yaw {yaw:+d} dz {dz:+d} tilt {tilt:+d} -> markers {sorted(dets)}")
            n += 1
    finally:
        c._send({"op": "move", "joints": {str(j): v for j, v in q_scan.items()}, "speed": MOVE_SPEED})
    used = sum(1 for d in data if len(d["corners"]) >= 2)
    print(f"done: {used} of {len(data)} views see 2+ markers. Next: python handeye.py solve {out.relative_to(HERE)}")


def cmd_collect(a) -> None:
    from arm_client import ArmClient
    if not a.run:
        raise SystemExit("moves the arm through ~30 viewpoints over the tray: add --run (arm_server.py running, "
                         "wrist_stream.py running, tray in view of the scan pose)")
    c = ArmClient()
    c.current_joints()
    q_scan = {int(k): float(v) for k, v in T.load_calib()["scan_pose"].items()}
    out = HERE / "logs" / time.strftime("handeye_%Y%m%d_%H%M%S")
    out.mkdir(parents=True)
    poses = [q for q in viewpoints(q_scan) if safe(q)]
    print(f"{len(poses)} viewpoints (above {MIN_TOOL_Z_MM:g} mm); photos in {out.relative_to(HERE)}")
    data = []
    try:
        for n, q in enumerate(poses):
            c._send({"op": "move", "joints": {str(j): v for j, v in q.items()}, "speed": MOVE_SPEED})
            t_done = time.time()
            time.sleep(SETTLE_S)
            frame = fresh_frame(t_done + SETTLE_S - 0.3)
            st = c.status() or {}
            q_meas = {j: float(st["state"][f"J{j}"]) for j in range(1, 7)}
            dets = {i: cc.tolist() for i, cc in T.detect_markers(frame) if i in IDS}
            cv2.imwrite(str(out / f"{n:02d}.jpg"), frame)
            data.append({"n": n, "q_cmd": q, "q_meas": q_meas, "corners": dets})
            print(f"  {n + 1:2d}/{len(poses)}: markers {sorted(dets)}")
            (out / "views.json").write_text(json.dumps(data))
    finally:
        c._send({"op": "move", "joints": {str(j): v for j, v in q_scan.items()}, "speed": MOVE_SPEED})
    used = sum(1 for d in data if len(d["corners"]) >= 2)
    print(f"done: {used} views with 2+ markers. Next: python handeye.py solve {out.relative_to(HERE)}")


# ------------------------------------------------------------------------------------------------- solve

def _unpack(x, n_mk):
    f, cx, cy, k1, k2 = x[:5]
    Ttc = rt_to_T(x[5:8], x[8:11])            # tool -> camera
    Tbt = rt_to_T(x[11:14], x[14:17])          # base -> tray
    mk = {IDS[0]: (0.0, 0.0, 0.0)}             # gauge: the first marker defines the tray frame
    for k, i in enumerate(IDS[1:]):
        mk[i] = tuple(x[17 + 3 * k:20 + 3 * k])
    return (f, cx, cy, k1, k2), Ttc, Tbt, mk


def _residuals(x, views):
    (f, cx, cy, k1, k2), Ttc, Tbt, mk = _unpack(x, len(IDS))
    res = []
    for v in views:
        Tcam = v["Tbase_tool"] @ Ttc
        Tinv = np.linalg.inv(Tcam)
        for i, px in v["corners"].items():
            P = (Tbt @ marker_T(*mk[i]) @ np.c_[SQUARE, np.ones(4)].T)    # 4 x 4, base frame
            Pc = (Tinv @ P)[:3].T
            res.append((project(Pc, f, cx, cy, k1, k2) - px).ravel())
    return np.concatenate(res)


def solve(views_path: Path) -> dict:
    from scipy.optimize import least_squares
    raw = json.loads(views_path.read_text())
    views = [{"Tbase_tool": tool_T({int(k): v for k, v in d["q_meas"].items()}),
              "corners": {int(i): np.asarray(c, float) for i, c in d["corners"].items()}}
             for d in raw if len(d["corners"]) >= 2]
    if len(views) < 8:
        raise SystemExit(f"only {len(views)} usable views (need 8+ with 2+ markers)")
    # initial guess: a 640x480 camera of ~65 deg, layout from the old tray calibration, then per-view PnP of the
    # tray + hand-eye (OpenCV) for the camera's place on the gripper
    f0, cx0, cy0 = 510.0, 320.0, 240.0
    K0 = K_of(f0, cx0, cy0)
    lay = {int(k): np.asarray(v, float) for k, v in T.load_calib()["layout_mm"].items()}
    o = lay[IDS[0]]
    d = lay[IDS[-1]] - o
    th0 = math.atan2(d[1], d[0])
    Rl = np.array([[math.cos(-th0), -math.sin(-th0)], [math.sin(-th0), math.cos(-th0)]])
    mk0 = {i: (*(Rl @ (lay[i] - o)), 0.0) for i in IDS}      # layout re-expressed with marker 10 at the origin
    Rg, tg, Rt, tt = [], [], [], []
    for v in views:
        obj, img = [], []
        for i, px in v["corners"].items():
            obj.append((marker_T(*mk0[i]) @ np.c_[SQUARE, np.ones(4)].T)[:3].T)
            img.append(px)
        ok, rv, tv = cv2.solvePnP(np.vstack(obj), np.vstack(img), K0, None, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            continue
        Rg.append(v["Tbase_tool"][:3, :3])
        tg.append(v["Tbase_tool"][:3, 3])
        Rt.append(cv2.Rodrigues(rv)[0])
        tt.append(tv.reshape(3))
    Rtc, ttc = cv2.calibrateHandEye(Rg, tg, Rt, tt, method=cv2.CALIB_HAND_EYE_PARK)
    Ttc0 = np.eye(4)
    Ttc0[:3, :3], Ttc0[:3, 3] = Rtc, ttc.reshape(3)
    Tbt_all = [v["Tbase_tool"] @ Ttc0 @ rt_to_T(cv2.Rodrigues(R)[0], t) for v, R, t in zip(views, Rt, tt)]
    Tbt0 = Tbt_all[len(Tbt_all) // 2]
    x0 = np.concatenate([[f0, cx0, cy0, 0.0, 0.0], T_to_rt(Ttc0)[0], T_to_rt(Ttc0)[1], T_to_rt(Tbt0)[0],
                         T_to_rt(Tbt0)[1], [val for i in IDS[1:] for val in mk0[i]]])
    sol = least_squares(_residuals, x0, args=(views,), loss="huber", f_scale=2.0, x_scale="jac", max_nfev=400)
    (f, cx, cy, k1, k2), Ttc, Tbt, mk = _unpack(sol.x, len(IDS))
    r = sol.fun.reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum(r * r, axis=1))))
    calib = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "views": len(views), "reprojection_rms_px": round(rms, 3),
             "lens": {"f": float(f), "cx": float(cx), "cy": float(cy), "k1": float(k1), "k2": float(k2)},
             "tool_to_camera": {"rvec": T_to_rt(Ttc)[0].tolist(), "t_mm": T_to_rt(Ttc)[1].tolist()},
             "marker_mm": MARKER_MM,
             "layout_mm": {int(i): [round(float(mk[i][0]), 3), round(float(mk[i][1]), 3)] for i in IDS}}
    # cross-check: every marker located from every view separately (the runtime method) -> spread around the mean
    spread = {}
    for i in IDS:
        pts = [locate_in_view(calib, v["Tbase_tool"], v["corners"][i]) for v in views if i in v["corners"]]
        if len(pts) >= 2:
            P = np.array([p for p, _ in pts])
            spread[i] = round(float(np.max(np.linalg.norm(P - P.mean(axis=0), axis=1))), 2)
    calib["locate_spread_mm"] = spread
    return calib


# ------------------------------------------------------------------------------------------------- locate

def load(path: Path = CALIB) -> dict:
    if not path.exists():
        raise SystemExit("no handeye.yaml: python handeye.py collect --run, then solve")
    return yaml.safe_load(path.read_text())


def locate_in_view(calib: dict, Tbase_tool, corners) -> tuple[np.ndarray, float]:
    """One marker's centre (mm, base frame) and heading (deg, its top edge direction in the base x-y plane) from its
    4 corner pixels seen with the gripper at Tbase_tool."""
    L = calib["lens"]
    K = K_of(L["f"], L["cx"], L["cy"])
    dist = np.array([L["k1"], L["k2"], 0.0, 0.0])
    ok, rv, tv = cv2.solvePnP(SQUARE, np.asarray(corners, float), K, dist, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    if not ok:
        raise ValueError("solvePnP failed")
    Ttc = rt_to_T(calib["tool_to_camera"]["rvec"], calib["tool_to_camera"]["t_mm"])
    Tbm = Tbase_tool @ Ttc @ rt_to_T(rv, tv)
    x_axis = Tbm[:3, 0]
    return Tbm[:3, 3], math.degrees(math.atan2(x_axis[1], x_axis[0]))


def locate(calib: dict, q_meas: dict, frame) -> dict[int, tuple[np.ndarray, float]]:
    """{marker id: (centre mm, heading deg)} for every tray marker in the frame."""
    Tbt = tool_T(q_meas)
    return {i: locate_in_view(calib, Tbt, c) for i, c in T.detect_markers(frame) if i in IDS}


def tray_from(calib: dict, found: dict[int, tuple[np.ndarray, float]]) -> dict:
    """Tray pose like markers.find_tray gives (xy, heading_deg, containers, seen, rms_mm), fitted to the located
    markers with the calibrated layout (a rigid 2-D fit; 2+ markers)."""
    lay = {int(k): np.asarray(v, float) for k, v in calib["layout_mm"].items()}
    ids = [i for i in sorted(found) if i in lay]
    if len(ids) < 2:
        raise ValueError(f"need 2+ tray markers, saw {ids}")
    c = np.mean([lay[i] for i in lay], axis=0)            # tray frame origin: the markers' centroid, as before
    lay_c = {i: lay[i] - c for i in lay}
    ang, t, rms = T.rigid_fit([lay_c[i] for i in ids], [found[i][0][:2] for i in ids])
    R = T.rot(ang)
    return {"xy": t, "heading_deg": ang, "rms_mm": rms, "seen": ids,
            "containers": {T.MARKERS[i]: R @ lay_c[i] + t for i in sorted(lay)},
            "z_mm": float(np.mean([found[i][0][2] for i in ids]))}


def cmd_locate(a) -> None:
    from arm_client import ArmClient
    calib = load()
    c = ArmClient()
    st = c.status() or {}
    q = {j: float(st["state"][f"J{j}"]) for j in range(1, 7)}
    found = locate(calib, q, fresh_frame(time.time() - 1.0))
    for i, (p, h) in sorted(found.items()):
        print(f"  marker {i}: ({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}) mm, heading {h:.1f} deg")
    if len(found) >= 2:
        tr = tray_from(calib, found)
        print(f"  tray at ({tr['xy'][0]:.1f}, {tr['xy'][1]:.1f}) heading {tr['heading_deg']:.1f} deg, fit "
              f"{tr['rms_mm']:.2f} mm")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["collect", "orbit", "solve", "locate"])
    ap.add_argument("path", nargs="?", help="solve: the logs/handeye_<time> folder")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "collect":
        cmd_collect(a)
    elif a.cmd == "orbit":
        cmd_orbit(a)
    elif a.cmd == "solve":
        folder = Path(a.path) if a.path else max((HERE / "logs").glob("handeye_*"), key=lambda p: p.stat().st_mtime)
        calib = solve(folder / "views.json")
        CALIB.write_text(yaml.safe_dump(calib, sort_keys=False))
        print(f"reprojection {calib['reprojection_rms_px']} px RMS over {calib['views']} views; lens f "
              f"{calib['lens']['f']:.1f} px; the same marker located from each view separately agrees within "
              f"{calib['locate_spread_mm']} mm (max from the mean). Saved {CALIB.name}")
    else:
        cmd_locate(a)


if __name__ == "__main__":
    main()
