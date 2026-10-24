"""Wrist-camera container finder: where are the 4 pour containers on the table right now?

The demo: judges move the containers anywhere inside the allowed area. The tubes stay in their fixed rack
(their grabs are recorded), only the containers move. On start the arm goes to one fixed SCAN POSE, finds every
container's ArUco marker, turns the markers into arm coordinates, and the usual moves pour into them. The gripper
never touches anything during a normal run.

How it works, in plain words
----------------------------
1. Each container sits on a flat card with its own ArUco marker (DICT_4X4_50) printed beside it, lying flat on the
   table: ids 10, 11, 12, 13 = containers 1-4 (configurable). The markers are never raised onto the containers, so
   every marker is on the same flat table plane.

2. The SCAN POSE is one fixed set of joint angles: arm high, wrist camera looking straight down at the area the
   containers can be in. While the arm sits in that pose the camera does not move, so it behaves exactly like a
   fixed overhead camera. For a fixed camera looking at a flat plane, pixel -> plane is a homography (a 3x3 matrix),
   so no hand-eye calibration is needed at all.

3. The homography is calibrated ONCE: spread the 4 containers to the corners of their area, touch the centre of
   each marker with the gripper tip (forward kinematics of the arm's joint angles gives the tip's (x, y) in mm),
   then take one photo from the scan pose. 4 pixel centres + 4 arm (x, y) -> cv2.findHomography. Redo it only if
   the camera mount or the scan pose changes.

4. To find containers later: detect the markers, push their 4 corners through the homography, and read off the
   marker's centre (x, y) in mm and its rotation on the table (the direction of its top edge).

5. If some marker is outside the picture, the arm turns ONLY the base (joint 1) to a few other angles and looks
   again. Joints 2-6 stay at the scan pose, so the camera has the same height and tilt; only its heading turned.
   The homography still maps pixels to the table as seen from the calibration heading, so the result is then
   rotated about the base's vertical axis by (J1 now - J1 at calibration). If a marker is seen at several stops,
   the detection closest to the image centre wins (least lens distortion, least perspective).

6. Pour spots are taught relative to each container's own marker ("the gripper sits 12 mm right and 55 mm in front
   of marker 11, heading 30 deg left of the marker's edge"), so wherever a judge puts the container, the spot moves
   and turns with it. well_target() gives that spot as {"xy_mm": [x, y], "R": 3x3} for motion.plan_travel.

7. Scan -> compute -> move. No live tracking while the arm moves. Every computed target is checked against the
   bench.yaml workspace box, the tube rack and the other containers before it is returned.

Lens calibration (checkerboard photos) is optional: without it everything still works, a little less accurately
near the image edges.

Units everywhere: joint angles in degrees {1..6} (follower frame, same as arm_server.py); positions in mm in the
arm base frame (x forward, y left, z up); angles on the table in degrees, counter-clockwise seen from above,
0 = +x.

Files
    robotics/vision_calib.yaml   lens (optional) + table homography + scan pose, written by calib-lens/calib-table
    robotics/spots.yaml          well_offsets: pour spots relative to each container's marker (teach-well)
    robotics/bench.yaml          optional `vision:` section overriding the defaults in DEFAULTS below
    robotics/vision_debug.jpg    annotated picture from `show`
    robotics/vision_markers.pdf  printable markers + checkerboard from `markers`

CLI (like move.py, nothing moves the arm without --run; arm_server.py must be running for arm ops):
    python vision.py calib-lens <folder of checkerboard photos>
    python vision.py calib-table --run         guided: scan pose, touch the 4 markers, photo
    python vision.py scan [--run]              container poses + pour targets (without --run: arm must already
                                               be at the scan pose, and no sweep)
    python vision.py check-sweep --run         same marker seen at two base angles: how far apart (mm)
    python vision.py teach-well well_2 --run   scan, then save the current pour pose relative to container 2
    python vision.py show                      annotated picture -> vision_debug.jpg
    python vision.py markers                   printable PDF of markers 10-13 + a checkerboard page
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path as FilePath

import cv2
import numpy as np
import yaml

import kinematics as K

HERE = FilePath(__file__).resolve().parent
CALIB_FILE = HERE / "vision_calib.yaml"
BENCH_FILE = HERE / "bench.yaml"
SPOTS_FILE = HERE / "spots.yaml"
DEBUG_IMAGE = HERE / "vision_debug.jpg"
MARKERS_PDF = HERE / "vision_markers.pdf"

# Every value can be overridden in bench.yaml under `vision:` (same keys).
DEFAULTS = {
    "marker_ids": {10: 1, 11: 2, 12: 3, 13: 4},   # ArUco id -> container number (well_N pours into container N)
    "marker_mm": 40.0,                            # printed marker side, black border included
    "camera": 2,                                  # wrist camera index (Innomaker U20CAM = DirectShow 2 here)
    "image_size": [640, 480],                     # what snap.py / the recorder use
    "sweep_stops_deg": [-40, -20, 0, 20, 40],     # base (J1) offsets from the scan pose's J1
    "settle_s": 0.5,                              # wait after a move before looking (arm vibration)
    "frames": 3,                                  # frames averaged per look
    "move_speed_deg_s": 20.0,                     # arm_server move speed to the scan pose / sweep stops
    "scan_pose_tol_deg": 1.0,                     # "already at the scan pose" tolerance per joint
    "container_radius_mm": 45.0,                  # footprint of a container around its pour spot / marker
    "tube_rack_mm": None,                         # {min: [x, y], max: [x, y]} rectangle the spot must avoid
    "table_error_warn_mm": 3.0,                   # calib-table: warn above this reprojection error
    "sweep_disagree_warn_mm": 2.0,                # check-sweep: warn above this
    "checkerboard": {"inner": [9, 6], "square_mm": 25.0},
}


class VisionError(RuntimeError):
    """Anything the vision step can't do safely: not calibrated, containers not visible, unsafe target."""


# =============================================================================================
# Config and calibration file
# =============================================================================================

def load_config(bench_path: FilePath = BENCH_FILE) -> dict:
    """DEFAULTS overridden by bench.yaml's optional `vision:` section.
    Output: dict with every key of DEFAULTS. Missing / unreadable bench.yaml -> plain defaults."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        bench = yaml.safe_load(bench_path.read_text()) or {}
    except OSError:
        bench = {}
    cfg.update(bench.get("vision") or {})
    cfg["marker_ids"] = {int(k): int(v) for k, v in cfg["marker_ids"].items()}
    cfg["workspace_mm"] = bench.get("workspace_mm")
    return cfg


def load_calib(path: FilePath = CALIB_FILE) -> dict:
    """vision_calib.yaml as a dict ({} if it doesn't exist yet). Sections: `lens`, `table`."""
    try:
        return yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}


def save_calib_section(name: str, section: dict, path: FilePath = CALIB_FILE) -> None:
    """Write one section (`lens` or `table`) of vision_calib.yaml, keeping the other one."""
    data = load_calib(path)
    data[name] = section
    path.write_text("# vision_calib.yaml: written by vision.py (calib-lens, calib-table). Do not edit by hand.\n"
                    + yaml.safe_dump(data, sort_keys=False))


# =============================================================================================
# ArUco: one place that copes with both the old (<4.7) and new (>=4.7) cv2.aruco APIs
# =============================================================================================

def _aruco():
    if not hasattr(cv2, "aruco"):
        raise VisionError("cv2.aruco missing: pip install opencv-contrib-python (it replaces opencv-python)")
    return cv2.aruco


def _dictionary():
    a = _aruco()
    if hasattr(a, "getPredefinedDictionary"):
        return a.getPredefinedDictionary(a.DICT_4X4_50)
    return a.Dictionary_get(a.DICT_4X4_50)            # old API


def detect_markers(image: np.ndarray) -> list[tuple[int, np.ndarray]]:
    """Every DICT_4X4_50 marker in an image.
    Input: BGR or grey image. Output: [(id, corners)], corners = 4x2 float pixels in ArUco order (the marker's own
    top-left, top-right, bottom-right, bottom-left). Empty list if none."""
    a = _aruco()
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if hasattr(a, "ArucoDetector"):
        corners, ids, _ = a.ArucoDetector(_dictionary(), a.DetectorParameters()).detectMarkers(gray)
    else:                                                # old API
        corners, ids, _ = a.detectMarkers(gray, _dictionary(), parameters=a.DetectorParameters_create())
    if ids is None:
        return []
    return [(int(i), c.reshape(4, 2).astype(np.float64)) for i, c in zip(ids.flatten(), corners)]


def marker_image(marker_id: int, side_px: int) -> np.ndarray:
    """The marker as a grey image side_px x side_px (black border included, no white margin)."""
    a = _aruco()
    if hasattr(a, "generateImageMarker"):
        return a.generateImageMarker(_dictionary(), marker_id, side_px)
    return a.drawMarker(_dictionary(), marker_id, side_px)  # old API


# =============================================================================================
# 1. Lens calibration (optional)
# =============================================================================================

def calibrate_lens(images: list[np.ndarray], inner=(9, 6), square_mm: float = 25.0) -> dict:
    """Camera matrix + distortion from checkerboard photos.

    Inputs
        images    : BGR photos of the printed checkerboard (from `markers`), different angles and positions,
                    all at the wrist camera's working resolution
        inner     : inner corners (columns, rows) of the board
        square_mm : printed square size; only scales the board, the lens result doesn't depend on it
    Output
        {"K": 3x3, "dist": [k1, k2, p1, p2, k3], "image_size": [w, h], "rms_px": float, "boards": n}
    Fails (VisionError) if fewer than 5 photos show the whole board: too few to pin the distortion down.
    """
    obj = np.zeros((inner[0] * inner[1], 3), np.float32)
    obj[:, :2] = np.mgrid[0:inner[0], 0:inner[1]].T.reshape(-1, 2) * square_mm
    obj_pts, img_pts, size = [], [], None
    for img in images:
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        size = gray.shape[::-1]
        ok, corners = cv2.findChessboardCorners(gray, tuple(inner))
        if not ok:
            continue
        # sub-pixel refinement: the coarse corners are only good to ~1 px, which shows up as distortion
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3))
        obj_pts.append(obj)
        img_pts.append(corners)
    if len(img_pts) < 5:
        raise VisionError(f"only {len(img_pts)} of {len(images)} photos show the whole {inner[0]}x{inner[1]} "
                          "board; need at least 5 (tilt it, move it to the corners)")
    rms, Kmat, dist, _, _ = cv2.calibrateCamera(obj_pts, img_pts, size, None, None)
    return {"K": Kmat.tolist(), "dist": dist.flatten().tolist(), "image_size": list(size),
            "rms_px": float(rms), "boards": len(img_pts)}


def undistort_points(px, lens: dict | None = None) -> np.ndarray:
    """Pixels as a perfect (distortion-free) lens would have seen them, still in pixels.

    Input: px = (N, 2) pixels (or one (2,) point); lens = calibrate_lens() output, or None to use
    vision_calib.yaml's. Output: (N, 2) pixels. Without a lens calibration the pixels come back unchanged.
    """
    pts = np.asarray(px, dtype=np.float64).reshape(-1, 2)
    lens = lens if lens is not None else load_calib().get("lens")
    if not lens:
        return pts
    Kmat, dist = np.array(lens["K"], np.float64), np.array(lens["dist"], np.float64)
    # P=K keeps the result in pixel units instead of normalised camera coordinates
    return cv2.undistortPoints(pts.reshape(-1, 1, 2), Kmat, dist, P=Kmat).reshape(-1, 2)


# =============================================================================================
# Base rotation (J1) and other small geometry
# =============================================================================================

def _base_axis():
    """(pivot (x, y) in mm, sign) of joint 1 from the URDF: a +1 deg J1 turns the arm sign*1 deg CCW seen from
    above, about the pivot. Read from kinematics.CHAIN so it can't disagree with fk()."""
    for _name, jtype, T0, axis in K.CHAIN:
        if jtype == "revolute":
            world_axis = T0[:3, :3] @ (axis / np.linalg.norm(axis))
            return T0[:2, 3] * 1000.0, float(np.sign(world_axis[2]))
    raise RuntimeError("no revolute joint in the URDF chain")


BASE_PIVOT_MM, BASE_SIGN = _base_axis()


def rotate_about_base(xy, delta_j1_deg: float) -> np.ndarray:
    """Where a point ends up if the base turns by delta_j1_deg of J1 (vertical axis, URDF pivot).
    Input: (x, y) mm or (N, 2). Output: same shape, mm."""
    a = math.radians(BASE_SIGN * delta_j1_deg)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    p = np.asarray(xy, dtype=np.float64)
    return (p - BASE_PIVOT_MM) @ R.T + BASE_PIVOT_MM


def rot2(deg: float) -> np.ndarray:
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])


def rotz(deg: float) -> np.ndarray:
    """3x3 rotation about vertical (z) by deg, CCW seen from above."""
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]])


def tool_heading_deg(R) -> float:
    """Heading of the gripper on the table, degrees CCW from +x.

    Uses the approach (z) axis projected onto the table: for the side grasp it is horizontal, and wrist-roll
    pouring turns about it, so the heading doesn't change while the tube tilts. If the approach axis is nearly
    vertical (top grasp) its projection is meaningless, so the tool's x axis is used instead."""
    R = np.asarray(R)
    v = R[:2, 2] if math.hypot(R[0, 2], R[1, 2]) > 0.3 else R[:2, 0]
    return math.degrees(math.atan2(v[1], v[0]))


def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _fk_xy_R(q) -> tuple[np.ndarray, np.ndarray]:
    """Gripper tip (x, y) in mm and tool rotation for joint angles q {1..6: deg}."""
    p_m, R = K.fk(q)
    return np.asarray(p_m[:2]) * 1000.0, np.asarray(R)


# =============================================================================================
# 2. Table calibration: pixel -> table mm homography at the scan pose
# =============================================================================================

@dataclass
class Table:
    """The table calibration. H maps (undistorted, if `undistorted`) pixels to arm (x, y) mm as seen from the
    scan pose with J1 = j1_calib_deg."""
    H: np.ndarray
    scan_pose: dict[int, float]
    j1_calib_deg: float
    undistorted: bool
    image_size: tuple[int, int]
    error_mm: float = 0.0

    @classmethod
    def load(cls, path: FilePath = CALIB_FILE) -> Table:
        """Fails (VisionError) if calib-table hasn't been done."""
        t = load_calib(path).get("table")
        if not t:
            raise VisionError("table not calibrated: run  python vision.py calib-table --run  first")
        return cls(np.array(t["H"], np.float64), {int(k): float(v) for k, v in t["scan_pose"].items()},
                   float(t["j1_calib_deg"]), bool(t["undistorted"]), tuple(t["image_size"]),
                   float(t.get("error_mm", 0.0)))

    def to_dict(self) -> dict:
        return {"H": self.H.tolist(), "scan_pose": {int(k): round(float(v), 3) for k, v in self.scan_pose.items()},
                "j1_calib_deg": round(float(self.j1_calib_deg), 3), "undistorted": self.undistorted,
                "image_size": list(self.image_size), "error_mm": round(float(self.error_mm), 3)}

    def px_to_mm(self, px, lens: dict | None = None) -> np.ndarray:
        """Pixels of the scan-pose photo -> (x, y) mm at J1 = j1_calib_deg. (N, 2) in, (N, 2) out."""
        pts = undistort_points(px, lens) if self.undistorted else np.asarray(px, np.float64).reshape(-1, 2)
        return cv2.perspectiveTransform(pts.reshape(-1, 1, 2), self.H).reshape(-1, 2)

    def mm_to_px(self, xy) -> np.ndarray:
        """Inverse of px_to_mm (in undistorted pixels when `undistorted`), for drawing."""
        pts = np.asarray(xy, np.float64).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(pts, np.linalg.inv(self.H)).reshape(-1, 2)


def calibrate_table(frame: np.ndarray, touch_xy: dict[int, tuple[float, float]], scan_pose: dict[int, float],
                    cfg: dict | None = None, lens: dict | None = None) -> tuple[Table, dict]:
    """Homography from one scan-pose photo and the touched marker centres.

    Inputs
        frame     : photo from the scan pose with all 4 markers in view (BGR)
        touch_xy  : {container number: (x, y) mm} gripper-tip positions touched on each marker's centre
        scan_pose : the scan pose joints {1..6: deg}; its J1 is the calibration heading
        cfg       : load_config() (marker ids and size)
        lens      : lens calibration or None (then pixels are used as they are)
    Output
        (Table, report) with report = {"error_mm", "max_error_mm", "per_marker": {...}, "warn": bool}
    Failure: VisionError if any of the 4 markers isn't in the photo or a touch point is missing.

    Why the error is not just "how well H fits the 4 points": with exactly 4 point pairs findHomography fits them
    exactly, so that residual is always 0. Instead the report maps each marker's 4 CORNERS through H and checks the
    sides come out the printed marker size: a wrong touch, a moved container or a tilted camera shows up there.
    """
    cfg = cfg or load_config()
    by_container = {cfg["marker_ids"][mid]: c for mid, c in detect_markers(frame) if mid in cfg["marker_ids"]}
    missing = sorted(set(cfg["marker_ids"].values()) - set(by_container))
    if missing:
        raise VisionError(f"calibration photo is missing container marker(s) {missing}: all 4 must be in view")
    if set(touch_xy) != set(by_container):
        raise VisionError(f"touch points for containers {sorted(touch_xy)}, need {sorted(by_container)}")
    ids = sorted(by_container)
    corners_px = {c: (undistort_points(by_container[c], lens) if lens else by_container[c]) for c in ids}
    src = np.array([corners_px[c].mean(axis=0) for c in ids])          # marker centre = mean of its corners
    dst = np.array([touch_xy[c] for c in ids], np.float64)
    H, _ = cv2.findHomography(src, dst, 0)
    if H is None:
        raise VisionError("homography failed: are 3 of the markers in a straight line?")
    h, w = frame.shape[:2]
    table = Table(H, dict(scan_pose), float(scan_pose[1]), bool(lens), (w, h))
    per = {}
    for c in ids:
        mm = cv2.perspectiveTransform(corners_px[c].reshape(-1, 1, 2), H).reshape(-1, 2)
        sides = np.linalg.norm(np.roll(mm, -1, axis=0) - mm, axis=1)
        per[c] = round(float(np.max(np.abs(sides - cfg["marker_mm"]))), 2)
    table.error_mm = float(np.mean(list(per.values())))
    report = {"error_mm": round(table.error_mm, 2), "max_error_mm": max(per.values()), "per_marker": per,
              "warn": max(per.values()) > cfg["table_error_warn_mm"]}
    return table, report


# =============================================================================================
# 3. Container detection
# =============================================================================================

@dataclass
class ContainerPose:
    """One container's marker on the table. x_mm, y_mm: marker centre; rotation_deg: direction of the marker's
    top edge (its own top-left -> top-right), CCW from +x. center_dist_px: how far from the image centre it was."""
    x_mm: float
    y_mm: float
    rotation_deg: float
    center_dist_px: float = 0.0
    frames: int = 1

    def tuple(self) -> tuple[float, float, float]:
        return (self.x_mm, self.y_mm, self.rotation_deg)


class Detections(dict):
    """{container number: ContainerPose} plus `.missing`: container numbers that were not seen."""

    def __init__(self, found: dict | None = None, expected=()):
        super().__init__(found or {})
        self.expected = sorted(expected)

    @property
    def missing(self) -> list[int]:
        return [c for c in self.expected if c not in self]

    def poses(self) -> dict[int, tuple[float, float, float]]:
        return {c: p.tuple() for c, p in sorted(self.items())}


def find_containers(frame: np.ndarray, j1_deg: float, table: Table | None = None, cfg: dict | None = None,
                    lens: dict | None = None) -> Detections:
    """Every container marker in one frame, in arm coordinates.

    Inputs
        frame  : wrist-camera image taken with joints 2-6 at the scan pose (BGR)
        j1_deg : the arm's J1 when the frame was taken (measured), degrees
        table  : Table.load() if None; cfg: load_config() if None; lens: vision_calib.yaml's if None
    Output
        Detections {container: ContainerPose}; `.missing` lists containers not in this frame. The plain
        {container: (x_mm, y_mm, rotation_deg)} view is `.poses()`.
    Failure: VisionError if the table isn't calibrated. A marker id seen twice keeps the one nearer the centre.
    """
    table = table or Table.load()
    cfg = cfg or load_config()
    if lens is None and table.undistorted:
        lens = load_calib().get("lens")
    h, w = frame.shape[:2]
    centre = np.array([w / 2.0, h / 2.0])
    delta = j1_deg - table.j1_calib_deg
    out = Detections(expected=cfg["marker_ids"].values())
    for mid, corners in detect_markers(frame):
        if mid not in cfg["marker_ids"]:
            continue
        cont = cfg["marker_ids"][mid]
        mm = table.px_to_mm(corners, lens)                     # 4 corners on the table, seen from J1 = calib
        # mapping all 4 corners and averaging them is better than mapping the pixel centre: under perspective the
        # pixel centroid of the corners isn't the image of the marker's centre
        mm = rotate_about_base(mm, delta)                      # the camera had turned with the base by delta
        c = mm.mean(axis=0)
        edge = mm[1] - mm[0]                                   # top-left -> top-right: the marker's own x axis
        pose = ContainerPose(float(c[0]), float(c[1]), math.degrees(math.atan2(edge[1], edge[0])),
                             float(np.linalg.norm(corners.mean(axis=0) - centre)))
        if cont not in out or pose.center_dist_px < out[cont].center_dist_px:
            out[cont] = pose
    return out


def average_detections(looks: list[Detections], expected) -> Detections:
    """Average several frames of the same view: mean x, y and a circular mean of the rotation (so 179 and -179
    average to 180, not 0). A container seen in only some frames is averaged over those."""
    out = Detections(expected=expected)
    for cont in expected:
        seen = [d[cont] for d in looks if cont in d]
        if not seen:
            continue
        rot = math.degrees(math.atan2(np.mean([math.sin(math.radians(p.rotation_deg)) for p in seen]),
                                      np.mean([math.cos(math.radians(p.rotation_deg)) for p in seen])))
        out[cont] = ContainerPose(float(np.mean([p.x_mm for p in seen])), float(np.mean([p.y_mm for p in seen])),
                                  rot, float(np.mean([p.center_dist_px for p in seen])), len(seen))
    return out


# =============================================================================================
# 4. Pour spots relative to each container's marker
# =============================================================================================

def _container_of(well: str) -> int:
    """"well_2" -> container 2."""
    try:
        n = int(well.split("_")[1])
    except (IndexError, ValueError):
        raise VisionError(f"well names look like well_1..well_4, got {well!r}") from None
    return n


def load_well_offsets(path: FilePath = SPOTS_FILE) -> dict[str, dict]:
    """spots.yaml well_offsets: {"well_2": {dx_mm, dy_mm, heading_offset_deg, R_taught, rotation_taught_deg}}."""
    try:
        return (yaml.safe_load(path.read_text()) or {}).get("well_offsets") or {}
    except OSError:
        return {}


def save_well_offset(name: str, offset: dict, path: FilePath = SPOTS_FILE) -> None:
    """Write one well offset into spots.yaml, keeping slots, wells and the header comments exactly as spots.py
    writes them (one pose per line)."""
    text = path.read_text() if path.exists() else ""
    data = yaml.safe_load(text) or {}
    data.setdefault("well_offsets", {})[name] = offset
    header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
    body = ""
    for grp in ("slots", "wells"):
        body += f"{grp}:\n"
        for spot, val in (data.get(grp) or {}).items():
            body += f"  {spot}: {'null' if val is None else '[' + ', '.join(str(v) for v in val) + ']'}\n"
    rest = {k: v for k, v in data.items() if k not in ("slots", "wells")}
    path.write_text(header + body + yaml.safe_dump(rest, sort_keys=False, default_flow_style=None))


def teach_well_offset(name: str, detections: Detections, q_pour: dict[int, float]) -> dict:
    """The pour pose as an offset in its container's marker frame.

    Inputs
        name       : "well_N": pours into container N
        detections : a scan of where the markers are right now
        q_pour     : the arm at the pour pose {1..6: deg}: tube held, tilted to a typical pour angle, lip over
                     container N (jogged there by hand)
    Output (also what save_well_offset stores)
        {"dx_mm", "dy_mm": gripper (x, y) relative to the marker centre, in the marker's own axes (its rotation
         undone), "heading_offset_deg": gripper heading minus marker rotation, "R_taught": tool rotation 3x3,
         "rotation_taught_deg": the marker's rotation at teach time}
    Failure: VisionError if container N isn't in the detections.
    """
    cont = _container_of(name)
    if cont not in detections:
        raise VisionError(f"container {cont} not found in the scan; can't teach {name}")
    m = detections[cont]
    g, R = _fk_xy_R(q_pour)
    # undo the marker's rotation: the offset is "forward / left of the marker" in the marker's own axes
    d = rot2(-m.rotation_deg) @ (g - np.array([m.x_mm, m.y_mm]))
    return {"dx_mm": round(float(d[0]), 2), "dy_mm": round(float(d[1]), 2),
            "heading_offset_deg": round(wrap_deg(tool_heading_deg(R) - m.rotation_deg), 2),
            "R_taught": np.round(R, 6).tolist(), "rotation_taught_deg": round(m.rotation_deg, 2)}


def _rect_contains(rect: dict, xy, margin: float = 0.0) -> bool:
    (x0, y0), (x1, y1) = rect["min"], rect["max"]
    return x0 - margin <= xy[0] <= x1 + margin and y0 - margin <= xy[1] <= y1 + margin


def _spot_xy(off: dict, m: ContainerPose) -> np.ndarray:
    return np.array([m.x_mm, m.y_mm]) + rot2(m.rotation_deg) @ np.array([off["dx_mm"], off["dy_mm"]])


def well_target(name: str, detections: Detections, R_carry=None, offsets: dict | None = None,
                cfg: dict | None = None) -> dict:
    """Where to travel to pour into container N, for motion.plan_travel.

    Inputs
        name       : "well_N"
        detections : scan() output
        R_carry    : the tool rotation the tube is carried with (fk of the current joints). Give it for
                     plan_travel: the returned R is then R_carry turned about vertical to the taught heading, so
                     travel never tilts the tube. Without it R is the taught (tilted, pouring) rotation turned
                     with the marker, which plan_travel refuses as a tilt change.
        offsets    : load_well_offsets() if None; cfg: load_config() if None
    Output
        {"xy_mm": [x, y], "R": 3x3 list}: marker position + the taught offset rotated by the marker's angle;
        heading rotated by the same angle about vertical.
    Failure (VisionError): offset not taught, container not found, or the spot is outside the bench.yaml
    workspace, inside the tube rack, or over another container (footprints from the `vision:` config).
    """
    cfg = cfg or load_config()
    offsets = load_well_offsets() if offsets is None else offsets
    cont = _container_of(name)
    if name not in offsets:
        raise VisionError(f"{name} has no taught offset: python vision.py teach-well {name} --run")
    if cont not in detections:
        raise VisionError(f"container {cont} wasn't found by the scan")
    off, m = offsets[name], detections[cont]
    xy = _spot_xy(off, m)
    heading = m.rotation_deg + off["heading_offset_deg"]
    if R_carry is not None:
        Rc = np.asarray(R_carry, np.float64)
        R = rotz(wrap_deg(heading - tool_heading_deg(Rc))) @ Rc
    else:
        R = rotz(m.rotation_deg - off["rotation_taught_deg"]) @ np.asarray(off["R_taught"], np.float64)
    check_target(xy, cont, detections, offsets, cfg)
    return {"xy_mm": [round(float(xy[0]), 2), round(float(xy[1]), 2)], "R": R.tolist()}


def check_target(xy, cont: int, detections: Detections, offsets: dict, cfg: dict) -> None:
    """Refuse (VisionError) a pour spot outside the workspace box, inside the tube rack, or over another
    container. Another container's footprint is a circle of container_radius_mm around ITS pour spot if taught
    (that's where its opening is), else around its marker."""
    ws = cfg.get("workspace_mm")
    if ws and not _rect_contains({"min": ws["min"][:2], "max": ws["max"][:2]}, xy):
        raise VisionError(f"pour spot ({xy[0]:.0f}, {xy[1]:.0f}) mm is outside the bench.yaml workspace box")
    rack = cfg.get("tube_rack_mm")
    if rack and _rect_contains(rack, xy, margin=cfg["container_radius_mm"] / 2):
        raise VisionError(f"pour spot ({xy[0]:.0f}, {xy[1]:.0f}) mm is over the tube rack")
    for other, m in detections.items():
        if other == cont:
            continue
        name = f"well_{other}"
        c = _spot_xy(offsets[name], m) if name in offsets else np.array([m.x_mm, m.y_mm])
        if np.linalg.norm(np.asarray(xy) - c) < cfg["container_radius_mm"]:
            raise VisionError(f"pour spot for container {cont} is over container {other} "
                              f"({np.linalg.norm(np.asarray(xy) - c):.0f} mm from it): containers too close")


# =============================================================================================
# 5 + 7. Scanning on the real arm (camera + arm_server), with the base sweep
# =============================================================================================

def grab_frames(camera: int, n: int = 3, size=(640, 480)) -> list[np.ndarray]:
    """n frames from the wrist camera, opened and released here (like snap.py: the camera is never held open
    between scans). 10 frames are dropped first so exposure settles. Failure: VisionError if no frame."""
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(camera, backend)
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        for _ in range(10):
            cap.read()
        frames = []
        for _ in range(n):
            ok, f = cap.read()
            if ok:
                frames.append(f)
    finally:
        cap.release()
    if not frames:
        raise VisionError(f"camera {camera} gave no frames (wrong index? unplugged?)")
    return frames


def _measured_j1(client) -> float:
    """The arm's measured J1 (what the encoder says), else its commanded J1."""
    st = client.status() or {}
    v = (st.get("state") or {}).get("J1")
    return float(v) if v is not None else float(client.current_joints()[1])


def _move_to(client, q: dict[int, float], cfg: dict) -> None:
    client._send({"op": "move", "joints": {str(j): round(float(q[j]), 3) for j in range(1, 7)},
                  "speed": cfg["move_speed_deg_s"]})


def _near(q_a: dict, q_b: dict, tol: float) -> bool:
    return max(abs(q_a[j] - q_b[j]) for j in range(1, 7)) <= tol


def look(client, table: Table, cfg: dict, lens: dict | None = None) -> Detections:
    """Wait for vibration to settle, grab cfg["frames"] frames, detect in each, average. J1 is read before and
    after the frames and averaged (it barely moves while holding)."""
    time.sleep(cfg["settle_s"])
    j1a = _measured_j1(client)
    frames = grab_frames(cfg["camera"], cfg["frames"], tuple(cfg["image_size"]))
    j1 = (j1a + _measured_j1(client)) / 2.0
    looks = [find_containers(f, j1, table, cfg, lens) for f in frames]
    return average_detections(looks, cfg["marker_ids"].values())


def scan(client, run: bool, sweep: bool = True, table: Table | None = None, cfg: dict | None = None,
         keep_all_stops: bool = False):
    """Find all 4 containers.

    Inputs
        client : arm_client.ArmClient (arm_server.py running)
        run    : False = never move the arm: the arm must already be at the scan pose, and no sweep happens
        sweep  : turn the base through cfg["sweep_stops_deg"] if containers are missing (needs run)
        keep_all_stops : check-sweep: visit every stop and also return the per-stop detections
    Output
        Detections with all 4 containers (or (Detections, {stop: Detections}) with keep_all_stops)
    Failure (VisionError): not calibrated, arm not at the scan pose without run, or any container still missing,
    named in the message. The arm is left at the scan pose.
    """
    table = table or Table.load()
    cfg = cfg or load_config()
    lens = load_calib().get("lens") if table.undistorted else None
    expected = list(cfg["marker_ids"].values())
    q = client.current_joints()
    if not _near(q, table.scan_pose, cfg["scan_pose_tol_deg"]):
        if not run:
            raise VisionError("the arm is not at the scan pose; add --run to move it there")
        _move_to(client, table.scan_pose, cfg)
    best = look(client, table, cfg, lens)
    per_stop = {0.0: Detections(dict(best), expected)}   # a copy: `best` gets merged into below
    if (best.missing or keep_all_stops) and sweep and run:
        for stop in cfg["sweep_stops_deg"]:
            if stop == 0:
                continue
            _move_to(client, {**table.scan_pose, 1: table.scan_pose[1] + stop}, cfg)
            d = look(client, table, cfg, lens)
            per_stop[float(stop)] = d
            for cont, pose in d.items():   # keep the view of each marker nearest the image centre
                if cont not in best or pose.center_dist_px < best[cont].center_dist_px:
                    best[cont] = pose
            if not best.missing and not keep_all_stops:
                break
        _move_to(client, table.scan_pose, cfg)
    if best.missing and not keep_all_stops:
        raise VisionError(f"can't see container(s) {best.missing}"
                          + ("" if sweep and run else " (no base sweep without --run)")
                          + ": check the markers are flat, unobstructed and inside the area")
    return (best, per_stop) if keep_all_stops else best


def sweep_disagreement(per_stop: dict[float, Detections]) -> dict[int, float]:
    """For every marker seen at two or more sweep stops: the largest distance (mm) between its positions.
    A well-calibrated table and base axis keep this under ~2 mm."""
    seen: dict[int, list] = {}
    for d in per_stop.values():
        for cont, p in d.items():
            seen.setdefault(cont, []).append(np.array([p.x_mm, p.y_mm]))
    return {c: round(max(float(np.linalg.norm(a - b)) for a in ps for b in ps), 2)
            for c, ps in seen.items() if len(ps) > 1}


# =============================================================================================
# Annotated picture and printable markers
# =============================================================================================

def annotate(frame: np.ndarray, j1_deg: float, table: Table, cfg: dict, offsets: dict,
             lens: dict | None = None) -> np.ndarray:
    """The frame with each marker outlined, its container number and (x, y, rotation), and the pour spot of
    every taught well drawn as a circle. Undistorted first when the table was calibrated on undistorted pixels,
    so the drawn spots land where the homography puts them."""
    img = frame.copy()
    if table.undistorted and lens:
        img = cv2.undistort(img, np.array(lens["K"]), np.array(lens["dist"]))
    dets = find_containers(frame, j1_deg, table, cfg, lens)
    for mid, corners in detect_markers(frame):
        if mid not in cfg["marker_ids"]:
            continue
        pts = (undistort_points(corners, lens) if table.undistorted else corners).astype(np.int32)
        cv2.polylines(img, [pts.reshape(-1, 1, 2)], True, (0, 220, 0), 2)
        cont = cfg["marker_ids"][mid]
        if cont in dets:
            p = dets[cont]
            cv2.putText(img, f"C{cont} ({p.x_mm:.0f},{p.y_mm:.0f}) {p.rotation_deg:.0f}deg",
                        tuple(int(v) for v in pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
    for name, off in offsets.items():
        cont = _container_of(name)
        if cont in dets:
            xy = rotate_about_base(_spot_xy(off, dets[cont]), -(j1_deg - table.j1_calib_deg))
            px = table.mm_to_px(xy)[0]
            cv2.circle(img, (int(px[0]), int(px[1])), 8, (255, 0, 255), 2)
            cv2.putText(img, name, (int(px[0]) + 10, int(px[1])), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)
    if dets.missing:
        cv2.putText(img, f"missing: {dets.missing}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    return img


def write_markers_pdf(path: FilePath, cfg: dict, dpi: int = 300) -> None:
    """Printable PDF at true size (print at 100 %, no 'fit to page'): page 1 = the 4 container markers with a
    white margin and labels; page 2 = the lens-calibration checkerboard. Pages are drawn with Pillow, written
    with matplotlib."""
    from PIL import Image, ImageDraw, ImageFont

    def mm(v: float) -> int:
        return int(round(v / 25.4 * dpi))

    try:
        font = ImageFont.load_default(size=mm(5))
    except TypeError:                                   # Pillow < 10.1 has no size argument
        font = ImageFont.load_default()
    page = Image.new("L", (mm(210), mm(297)), 255)       # A4 portrait
    draw = ImageDraw.Draw(page)
    side = cfg["marker_mm"]
    for k, (mid, cont) in enumerate(sorted(cfg["marker_ids"].items())):
        x0, y0 = mm(25 + (k % 2) * 90), mm(30 + (k // 2) * 110)
        page.paste(Image.fromarray(marker_image(mid, mm(side))), (x0, y0))
        draw.text((x0, y0 + mm(side) + mm(4)), f"container {cont}  (ArUco 4x4_50 id {mid}, {side:g} mm)",
                  fill=0, font=font)
    draw.text((mm(25), mm(12)), "Laminar container markers - print at 100 %, check the side is "
              f"{side:g} mm", fill=0, font=font)
    inner, sq = cfg["checkerboard"]["inner"], cfg["checkerboard"]["square_mm"]
    board = Image.new("L", (mm(297), mm(210)), 255)       # A4 landscape
    bd = ImageDraw.Draw(board)
    cols, rows = inner[0] + 1, inner[1] + 1
    ox, oy = (mm(297) - mm(cols * sq)) // 2, (mm(210) - mm(rows * sq)) // 2
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                bd.rectangle([ox + mm(c * sq), oy + mm(r * sq), ox + mm((c + 1) * sq) - 1,
                              oy + mm((r + 1) * sq) - 1], fill=0)
    bd.text((mm(10), mm(5)), f"checkerboard {inner[0]}x{inner[1]} inner corners, {sq:g} mm squares", fill=0,
            font=font)
    # matplotlib writes each page at its true physical size (Pillow's PDF writer needs a JPEG codec that some
    # builds lack); figimage places the pixels 1:1 at `dpi`, so a marker prints at exactly marker_mm
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import PdfPages
    import matplotlib.pyplot as plt
    with PdfPages(path) as pdf:
        for im in (page, board):
            fig = plt.figure(figsize=(im.width / dpi, im.height / dpi), dpi=dpi)
            fig.figimage(np.asarray(im), 0, 0, cmap="gray", vmin=0, vmax=255)
            pdf.savefig(fig, dpi=dpi)
            plt.close(fig)


# =============================================================================================
# CLI
# =============================================================================================

def _client():
    from arm_client import ArmClient
    c = ArmClient()
    c.current_joints()                      # fails clearly if arm_server.py isn't running
    return c


def _print_dets(dets: Detections, offsets: dict, cfg: dict, R_carry=None) -> None:
    for cont, p in sorted(dets.items()):
        line = f"  container {cont}: x={p.x_mm:7.1f} y={p.y_mm:7.1f} mm  rot={p.rotation_deg:6.1f} deg"
        name = f"well_{cont}"
        if name in offsets:
            try:
                t = well_target(name, dets, R_carry=R_carry, offsets=offsets, cfg=cfg)
                line += f"  | {name} pour target ({t['xy_mm'][0]:.1f}, {t['xy_mm'][1]:.1f}) mm"
            except VisionError as e:
                line += f"  | {name}: {e}"
        else:
            line += f"  | {name}: not taught"
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["calib-lens", "calib-table", "scan", "check-sweep", "teach-well", "show",
                                   "markers"])
    ap.add_argument("arg", nargs="?", help="calib-lens: photo folder; teach-well: well_1..well_4")
    ap.add_argument("--run", action="store_true", help="allow the arm to move")
    a = ap.parse_args()
    cfg = load_config()
    try:
        if a.op == "markers":
            write_markers_pdf(MARKERS_PDF, cfg)
            print(f"wrote {MARKERS_PDF}: print at 100 % and check a marker measures {cfg['marker_mm']:g} mm")

        elif a.op == "calib-lens":
            folder = FilePath(a.arg or "")
            files = sorted(p for p in folder.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
            if not files:
                raise VisionError(f"no .jpg/.png photos in {folder}")
            lens = calibrate_lens([cv2.imread(str(p)) for p in files], tuple(cfg["checkerboard"]["inner"]),
                                  cfg["checkerboard"]["square_mm"])
            save_calib_section("lens", lens)
            print(f"lens: {lens['boards']} boards, RMS {lens['rms_px']:.2f} px -> {CALIB_FILE}")
            if load_calib().get("table"):
                print("NOTE: the table was calibrated without this lens model: redo calib-table to use it")

        elif a.op == "calib-table":
            client = _client()
            print("STEP 1. Put the arm in the SCAN POSE: high, wrist camera pointing straight down over the area "
                  "the containers may be in (jog it with move_tool.py / arm_cmd.py).")
            input("        Press Enter when it is there ... ")
            scan_pose = client.current_joints()
            print(f"        scan pose = {scan_pose}")
            touch = {}
            for cont in sorted(cfg["marker_ids"].values()):
                print(f"STEP 2.{cont} Touch the CENTRE of container {cont}'s marker with the gripper tip.")
                input("        Press Enter when touching ... ")
                xy, _ = _fk_xy_R(client.current_joints())
                touch[cont] = (float(xy[0]), float(xy[1]))
                print(f"        container {cont} marker at ({xy[0]:.1f}, {xy[1]:.1f}) mm")
            print("STEP 3. Back to the scan pose for the photo (lift the gripper clear first if it is low).")
            if a.run:
                _move_to(client, scan_pose, cfg)
            else:
                input("        (no --run) put the arm back in the scan pose yourself, then press Enter ... ")
                if not _near(client.current_joints(), scan_pose, cfg["scan_pose_tol_deg"]):
                    raise VisionError("the arm is not back at the scan pose")
            time.sleep(cfg["settle_s"])
            frame = grab_frames(cfg["camera"], 1, tuple(cfg["image_size"]))[0]
            lens = load_calib().get("lens")
            table, rep = calibrate_table(frame, touch, scan_pose, cfg, lens)
            save_calib_section("table", table.to_dict())
            print(f"saved {CALIB_FILE}: marker size check error {rep['error_mm']:.2f} mm mean, "
                  f"{rep['max_error_mm']:.2f} mm worst {rep['per_marker']}")
            if rep["warn"]:
                print(f"WARNING: above {cfg['table_error_warn_mm']:g} mm: a touch was off-centre, a container "
                      "moved, or the camera isn't looking straight down. Redo it.")

        elif a.op in ("scan", "check-sweep"):
            client = _client()
            if a.op == "check-sweep" and not a.run:
                raise VisionError("check-sweep turns the base: add --run")
            if a.op == "scan":
                dets = scan(client, run=a.run)
                print("containers:")
                _print_dets(dets, load_well_offsets(), cfg)
            else:
                dets, per_stop = scan(client, run=True, keep_all_stops=True)
                dis = sweep_disagreement(per_stop)
                for cont, d in sorted(dis.items()):
                    flag = "  WARNING" if d > cfg["sweep_disagree_warn_mm"] else ""
                    print(f"  container {cont}: seen at {sum(cont in v for v in per_stop.values())} stops, "
                          f"disagree by {d:.2f} mm{flag}")
                if not dis:
                    print("  no marker was seen at two stops: move a container so it is in two views")

        elif a.op == "teach-well":
            if not a.arg:
                raise VisionError("teach-well needs a name: well_1..well_4")
            client = _client()
            dets = scan(client, run=a.run)
            print(f"scanned: {dets.poses()}")
            print(f"Now jog the arm to the pour pose for {a.arg}: tube held, tilted to a typical pour angle, "
                  "lip over the container.")
            input("Press Enter when it is there ... ")
            off = teach_well_offset(a.arg, dets, client.current_joints())
            save_well_offset(a.arg, off)
            print(f"saved {a.arg} offset {off['dx_mm']:+.1f}, {off['dy_mm']:+.1f} mm in the marker frame, "
                  f"heading {off['heading_offset_deg']:+.1f} deg -> {SPOTS_FILE}")

        elif a.op == "show":
            table = Table.load()
            client = _client()
            if not a.run and not _near({**client.current_joints(), 1: table.scan_pose[1]}, table.scan_pose,
                                       cfg["scan_pose_tol_deg"]):
                raise VisionError("joints 2-6 are not at the scan pose; add --run to move there")
            if a.run:
                _move_to(client, table.scan_pose, cfg)
            time.sleep(cfg["settle_s"])
            j1 = _measured_j1(client)
            frame = grab_frames(cfg["camera"], 1, tuple(cfg["image_size"]))[0]
            lens = load_calib().get("lens") if table.undistorted else None
            cv2.imwrite(str(DEBUG_IMAGE), annotate(frame, j1, table, cfg, load_well_offsets(), lens))
            print(f"wrote {DEBUG_IMAGE}")
    except (VisionError, RuntimeError) as e:
        sys.exit(f"vision: {e}")


if __name__ == "__main__":
    main()
