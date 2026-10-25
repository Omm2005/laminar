"""markers.py without hardware: real ArUco markers rendered through a known camera homography."""
import math

import cv2
import numpy as np

import markers as T
from vision import marker_image

# a camera looking down at the table, slightly tilted: table mm -> image px (640 x 480)
H_CAM = np.array([[0.0, -1.2, 320.0], [-1.2, 0.0, 600.0], [0.0, 0.0003, 1.0]])
# the tray: 4 markers in a row, 70 mm apart, slightly staggered (layout in the tray frame, mm)
LAYOUT = {10: (-105.0, 5.0), 11: (-35.0, -5.0), 12: (35.0, 5.0), 13: (105.0, -5.0)}


def tray_markers(xy, heading_deg):
    """Table (x, y) of each marker centre for a tray at xy, turned by heading_deg."""
    R = T.rot(heading_deg)
    return {i: R @ np.array(p) + np.asarray(xy, float) for i, p in LAYOUT.items()}


def render(centres, heading_deg, ids):
    """A camera image with the given markers lying flat on the table, rotated with the tray."""
    img = np.full((480, 640), 255, np.uint8)
    h = T.MARKER_MM / 2
    for i in ids:
        R = T.rot(heading_deg)
        corners_mm = [centres[i] + R @ np.array(d) for d in ((-h, h), (h, h), (h, -h), (-h, -h))]
        corners_px = T.mm_to_px(np.linalg.inv(H_CAM), corners_mm).astype(np.float32)
        side = 200
        src = np.float32([[0, 0], [side, 0], [side, side], [0, side]])
        warp = cv2.warpPerspective(marker_image(i, side), cv2.getPerspectiveTransform(src, corners_px), (640, 480),
                                   borderValue=255)
        mask = cv2.warpPerspective(np.full((side, side), 255, np.uint8),
                                   cv2.getPerspectiveTransform(src, corners_px), (640, 480))
        img[mask > 128] = warp[mask > 128]
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def calib_for(layout_xy):
    return {"homography": np.linalg.inv(H_CAM), "layout_mm": T.tray_layout(layout_xy), "table_z_mm": 0.0}


def test_marker_centre_is_the_diagonal_crossing():
    c = np.array([[0, 0], [10, 0], [12, 12], [0, 10]], float)
    p = T.marker_center(c)
    assert abs(np.cross(c[2] - c[0], p - c[0])) < 1e-9 and abs(np.cross(c[3] - c[1], p - c[1])) < 1e-9


def test_rigid_fit_recovers_a_turn_and_a_shift():
    src = np.array([[0, 0], [50, 0], [50, 20]], float)
    dst = src @ T.rot(30).T + [100, -40]
    ang, t, rms = T.rigid_fit(src, dst)
    assert abs(ang - 30) < 1e-9 and np.allclose(t, [100, -40]) and rms < 1e-9


def test_tray_found_with_only_two_markers_visible():
    home = tray_markers((300, 0), 0)
    calib = calib_for(home)
    moved = tray_markers((320, 60), 25)
    frame = render(moved, 25, ids=[11, 13])          # 10 and 12 hidden
    pose = T.find_tray(frame, calib)
    assert pose["seen"] == [11, 13]
    for i, cont in T.MARKERS.items():                 # hidden containers placed too, within 1.5 mm
        assert np.linalg.norm(pose["containers"][cont] - moved[i]) < 1.5, (i, pose["containers"][cont], moved[i])
    d = moved[13] - moved[10]                          # the tray's heading = the line from marker 10 to 13
    assert abs(((pose["heading_deg"] - math.degrees(math.atan2(d[1], d[0]))) + 180) % 360 - 180) < 1.0


def test_one_marker_is_not_enough():
    calib = calib_for(tray_markers((300, 0), 0))
    frame = render(tray_markers((300, 0), 0), 0, ids=[12])
    try:
        T.find_tray(frame, calib)
    except ValueError as e:
        assert "at least 2" in str(e)
    else:
        raise AssertionError("a single marker must not give a tray pose")


def test_calibration_from_two_tray_positions():
    rng = np.random.default_rng(0)
    touches, photos = [], []
    for pos, (xy, hd) in enumerate([((280, -30), 0), ((340, 80), 40)], start=1):
        true = tray_markers(xy, hd)
        frame = render(true, hd, ids=list(T.MARKERS))
        photos.append({"pos": pos, "corners": {i: c for i, c in T.detect_markers(frame) if i in T.MARKERS}})
        for i, p in true.items():                     # touches land within ~1 mm of the true centre
            touches.append({"pos": pos, "id": i, "xy_mm": list(p + rng.normal(0, 0.7, 2)), "z_mm": 12.0})
    calib, rep = T.calibrate_from(touches, photos)
    assert len(rep["points"]) == 8 and rep["homography_rms_mm"] < 2.5 and rep["tray_rigid_rms_mm"] < 2.0
    assert abs(calib["table_z_mm"] - 12.0) < 1e-9
    # the calibrated map then finds a third tray position
    moved = tray_markers((300, 30), -20)
    pose = T.find_tray(render(moved, -20, ids=[10, 12, 13]), calib)
    for i, cont in T.MARKERS.items():
        assert np.linalg.norm(pose["containers"][cont] - moved[i]) < 3.0


def test_point_down_aims_the_tool_axis_at_the_table():
    R = cv2.Rodrigues(np.array([0.4, -0.9, 0.3]))[0]
    D = T.point_down(R)
    assert np.allclose(D[:, T.APPROACH], [0, 0, -1]) and np.allclose(D @ D.T, np.eye(3))
    assert math.isclose(np.linalg.det(D), 1.0)


def test_tray_outside_the_scan_view_found_with_the_base_turned():
    from vision import rotate_about_base
    calib = calib_for(tray_markers((300, 0), 0))
    true = tray_markers((260, 150), 50)               # off to the side: only seen after turning the base
    dj1 = 30.0
    # with the base turned by dj1 the camera sees the table as if it had turned the other way
    in_view = {i: rotate_about_base(p, -dj1) for i, p in true.items()}
    d = in_view[13] - in_view[10]
    frame = render(in_view, math.degrees(math.atan2(d[1], d[0])) - math.degrees(math.atan2(-10, 210)), ids=[10, 11, 13])
    pose = T.find_tray(frame, calib, dj1)
    for i, cont in T.MARKERS.items():
        assert np.linalg.norm(pose["containers"][cont] - true[i]) < 2.0, (i, pose["containers"][cont], true[i])


def test_lock_in_averages_two_detections():
    calib = calib_for(tray_markers((300, 0), 0))
    true = tray_markers((310, 20), 15)
    a = T.find_tray(render(true, 15, ids=[10, 11, 12, 13]), calib)
    b = T.find_tray(render(true, 15, ids=[11, 13]), calib)
    pose = T.lock_in([a, b])
    assert pose["seen"] == [10, 11, 12, 13] and pose["spread_mm"] < 2.0
    for i, cont in T.MARKERS.items():
        assert np.linalg.norm(pose["containers"][cont] - true[i]) < 1.5


class FakeArm:
    """Commanded joints move instantly; the measured joints sag like the real arm under load."""
    SAG = {1: 0.0, 2: 0.5, 3: -2.5, 4: -1.8, 5: 0.0, 6: 0.0}

    def __init__(self, q):
        self.cmd, self.runs = dict(q), 0

    def current_joints(self):
        return dict(self.cmd)

    def status(self):
        return {"state": {f"J{j}": self.cmd[j] + self.SAG[j] for j in range(1, 7)}}

    def run(self, path):
        assert max(abs(path.joints[0][j] - self.cmd[j]) for j in range(1, 7)) < 1.0  # starts where the arm is
        self.cmd, self.runs = dict(path.final_joints), self.runs + 1
        return {"result": "ok"}


def test_settle_on_cancels_a_steady_sag(monkeypatch):
    import motion as M
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    q0 = {1: -30.0, 2: 110.0, 3: 60.0, 4: 30.0, 5: 0.0, 6: 0.0}
    target, _ = M.fk_mm(q0)
    err, offset = T.settle_on(FakeArm(q0), target, log=lambda *a: None)
    assert err <= T.CORRECT_TOL_MM and np.linalg.norm(offset) > 10   # the command had to move to cancel the sag


def test_every_container_lands_on_target_and_the_sag_is_learned(monkeypatch):
    import motion as M
    from spots import get_spot
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    q_scan = get_spot("vial_A_initial_position")
    conts = {k: np.array([300.0, (k - 2.5) * 60]) for k in (1, 2, 3, 4)}
    plans, _ = T.plan_visit(q_scan, conts, 64.0)   # the real table height
    arm, learned, runs = FakeArm(q_scan), np.zeros(3), []
    for n, (cont, plan) in enumerate(plans):
        before = arm.runs
        learned = T.run_container(arm, n, plan, None, learned, confirm=lambda *a: None, log=lambda *a: None)
        runs.append(arm.runs - before)
        p, _ = M.fk_mm({j: float(v) for j, v in {int(k[1:]): x for k, x in arm.status()["state"].items()}.items()})
        assert np.linalg.norm(p - M.fk_mm(plan[-1].final_joints)[0]) <= T.CORRECT_TOL_MM + 0.5, cont
    assert runs[-1] <= runs[0]   # pre-compensated from the last stop: no more correction moves than the first


def test_untouched_photos_fix_the_map_away_from_the_touched_spot():
    rng = np.random.default_rng(3)
    line = math.degrees(math.atan2(LAYOUT[13][1] - LAYOUT[10][1], LAYOUT[13][0] - LAYOUT[10][0]))
    def photo(pos, xy, hd):    # markers drawn square to the 10 -> 13 line, as on the real tray
        true = tray_markers(xy, hd)
        frame = render(true, hd + line, ids=list(T.MARKERS))
        return true, {"pos": pos, "corners": {i: c for i, c in T.detect_markers(frame) if i in T.MARKERS}}
    true1, ph1 = photo(1, (290, -60), 10)            # the only touched position
    touches = [{"pos": 1, "id": i, "xy_mm": list(p), "z_mm": 0.0} for i, p in true1.items()]  # exact touches:
    # this checks the method; with real touches (~0.5 mm noise, one position) the far spot came out 3.1 vs 4.5 mm
    far = tray_markers((370, 110), -35)              # a spot far from it
    test_frame = render(far, -35 + line, ids=list(T.MARKERS))
    def worst(calib):
        pose = T.find_tray(test_frame, calib)
        return max(np.linalg.norm(pose["containers"][T.MARKERS[i]] - far[i]) for i in far)
    only_touched, _ = T.calibrate_from(touches, [ph1])
    shots = [photo(10 + k, xy, hd)[1] for k, (xy, hd) in enumerate([((330, 60), 30), ((280, 120), -60),
                                                                      ((360, -20), 80), ((320, 160), 0)])]
    with_shape, rep = T.calibrate_from(touches, [ph1] + shots)
    assert worst(with_shape) < 1.0 < worst(only_touched)
    lo, hi = rep["marker_side_mm"]
    assert 18.0 < lo and hi < 21.0   # ArUco corners on small markers sit ~1 px inside: sides read a bit short,
                                     # symmetrically, so the centres (what matters) stay exact
