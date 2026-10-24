"""vision.py without a camera or an arm: synthetic wrist-camera images with ArUco markers drawn at known table
positions through a known pixel <-> mm homography.

Run from robotics/:   python -m pytest -q tests
"""
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROBOTICS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROBOTICS))
import vision as V  # noqa: E402

W, H_IMG = 640, 480
# Ground truth: table mm (x forward, y left) -> wrist-camera pixels, camera looking down with "forward" up in the
# image, 1.6 px/mm, plus a little perspective so the homography is not just a similarity.
A = np.array([[0.0, -1.6, 320.0], [-1.6, 0.0, 240.0 + 1.6 * 320.0], [0.0, 0.0, 1.0]])
P = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [2e-4, 0.0, 1.0 - 2e-4 * 320.0]])
H_TRUE = A @ P                          # mm -> px
CFG = {**V.DEFAULTS, "workspace_mm": {"min": [-400, -400, 0], "max": [400, 400, 450]}}
SCAN_POSE = {1: 0.0, 2: 90.0, 3: 30.0, 4: 60.0, 5: 0.0, 6: 0.0}
# container: (x_mm, y_mm, rotation_deg) of its marker
TRUTH = {1: (260.0, -100.0, 0.0), 2: (390.0, -90.0, 30.0), 3: (270.0, 100.0, -60.0), 4: (380.0, 110.0, 135.0)}
ID_OF = {c: m for m, c in V.DEFAULTS["marker_ids"].items()}


def mm_to_px(xy):
    return cv2.perspectiveTransform(np.asarray(xy, np.float64).reshape(-1, 1, 2), H_TRUE).reshape(-1, 2)


def marker_corners_mm(x, y, rot_deg, side=40.0):
    """Corners (marker's own TL, TR, BR, BL) on the table for a marker centred at (x, y) whose top edge points
    along rot_deg. The 'down' side is chosen so the marker is not mirrored in the image (a mirrored ArUco marker
    is a different, invalid code, exactly as a real printed marker can't be mirrored)."""
    u = np.array([math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))])
    for down in (np.array([u[1], -u[0]]), np.array([-u[1], u[0]])):
        c = np.array([x, y])
        tl = c - u * side / 2 - down * side / 2
        mm = np.array([tl, tl + u * side, tl + u * side + down * side, tl + down * side])
        px = mm_to_px(mm)
        area = sum(px[i][0] * px[(i + 1) % 4][1] - px[(i + 1) % 4][0] * px[i][1] for i in range(4))
        if area > 0:   # clockwise on screen (image y points down): the way the marker is printed
            return mm
    raise AssertionError("no unmirrored orientation")


def render(markers: dict[int, tuple[float, float, float]]) -> np.ndarray:
    """A white table photo with each container's marker warped to its true pixel quadrilateral."""
    img = np.full((H_IMG, W, 3), 255, np.uint8)
    side_px = 240
    for cont, (x, y, rot) in markers.items():
        mk = cv2.cvtColor(V.marker_image(ID_OF[cont], side_px), cv2.COLOR_GRAY2BGR)
        dst = mm_to_px(marker_corners_mm(x, y, rot)).astype(np.float32)
        src = np.float32([[0, 0], [side_px, 0], [side_px, side_px], [0, side_px]])
        warped = cv2.warpPerspective(mk, cv2.getPerspectiveTransform(src, dst), (W, H_IMG),
                                     flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255))
        mask = np.zeros((H_IMG, W), np.uint8)
        cv2.fillConvexPoly(mask, dst.astype(np.int32), 255)
        img[mask > 0] = warped[mask > 0]
    return img


@pytest.fixture(scope="module")
def table():
    t, rep = V.calibrate_table(render(TRUTH), {c: v[:2] for c, v in TRUTH.items()}, SCAN_POSE, CFG, lens=None)
    t.report = rep
    return t


def test_homography_maps_markers_back_within_1mm(table):
    for mid, corners in V.detect_markers(render(TRUTH)):
        cont = CFG["marker_ids"][mid]
        xy = table.px_to_mm(corners).mean(axis=0)
        assert np.linalg.norm(xy - TRUTH[cont][:2]) < 1.0
    # sides map back to 40 mm within the warning threshold (the synthetic image rounds corners to whole pixels,
    # ~0.6 mm each at 1.6 px/mm)
    assert table.report["max_error_mm"] < CFG["table_error_warn_mm"] and not table.report["warn"]


def test_find_containers_recovers_positions_and_rotations(table):
    dets = V.find_containers(render(TRUTH), 0.0, table, CFG, lens=None)
    assert dets.missing == []
    for cont, (x, y, rot) in TRUTH.items():
        p = dets[cont]
        assert math.hypot(p.x_mm - x, p.y_mm - y) < 1.0
        assert abs(V.wrap_deg(p.rotation_deg - rot)) < 1.5


def test_missing_containers_are_reported(table):
    dets = V.find_containers(render({c: TRUTH[c] for c in (1, 2, 4)}), 0.0, table, CFG, lens=None)
    assert dets.missing == [3] and sorted(dets) == [1, 2, 4]


def test_base_rotation_matches_forward_kinematics():
    q = {1: 0.0, 2: 80.0, 3: 20.0, 4: 70.0, 5: 0.0, 6: 0.0}
    xy0, _ = V._fk_xy_R(q)
    xy1, _ = V._fk_xy_R({**q, 1: 25.0})
    assert np.linalg.norm(V.rotate_about_base(xy0, 25.0) - xy1) < 0.01


@pytest.mark.parametrize("delta", [-20.0, -10.0, 10.0, 20.0])   # larger turns carry this marker out of view
def test_marker_seen_at_two_base_angles_maps_to_the_same_arm_xy(table, delta):
    # with the base turned by delta, a marker at world P appears where P rotated by -delta would at calibration
    world = {2: (330.0, -20.0, 30.0)}
    seen = {c: (*V.rotate_about_base((x, y), -delta), rot - V.BASE_SIGN * delta) for c, (x, y, rot) in world.items()}
    at_calib = V.find_containers(render(world), 0.0, table, CFG, lens=None)
    turned = V.find_containers(render(seen), delta, table, CFG, lens=None)
    a, b = at_calib[2], turned[2]
    assert math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm) < 1.0
    assert abs(V.wrap_deg(a.rotation_deg - b.rotation_deg)) < 1.5


def _dets(poses):
    return V.Detections({c: V.ContainerPose(*p) for c, p in poses.items()}, expected=[1, 2, 3, 4])


def test_well_target_rotates_the_taught_offset_with_the_marker():
    q_pour = {1: -10.0, 2: 70.0, 3: 30.0, 4: 70.0, 5: 0.0, 6: -20.0}
    g, R_pour = V._fk_xy_R(q_pour)
    m0 = (g[0] - 30.0, g[1] + 40.0, 0.0)                         # marker beside the gripper at teach time
    off = V.teach_well_offset("well_2", _dets({2: m0}), q_pour)
    assert off["dx_mm"] == pytest.approx(30.0, abs=0.01) and off["dy_mm"] == pytest.approx(-40.0, abs=0.01)
    offsets = {"well_2": off}
    # same marker pose -> exactly the taught gripper (x, y)
    wide = {**CFG, "workspace_mm": {"min": [-600, -600, 0], "max": [600, 600, 450]}}
    t = V.well_target("well_2", _dets({2: m0}), offsets=offsets, cfg=wide)
    assert np.allclose(t["xy_mm"], g, atol=0.02)
    # container moved and turned 90 deg: offset (30, -40) becomes (40, 30) from the new marker centre
    m1 = (300.0, 50.0, 90.0)
    R_carry = V.rotz(0.0) @ np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], float)   # side grasp, approach along +x
    t = V.well_target("well_2", _dets({2: m1}), R_carry=R_carry, offsets=offsets, cfg=wide)
    assert np.allclose(t["xy_mm"], [340.0, 80.0], atol=0.02)
    want = V.wrap_deg(90.0 + off["heading_offset_deg"])
    assert abs(V.wrap_deg(V.tool_heading_deg(t["R"]) - want)) < 1e-6
    # only a turn about vertical: the tube's tilt is unchanged
    assert np.allclose(np.asarray(t["R"])[2], R_carry[2], atol=1e-9)


def test_well_target_refuses_unsafe_spots():
    offsets = {"well_1": {"dx_mm": 0.0, "dy_mm": 60.0, "heading_offset_deg": 0.0,
                          "R_taught": np.eye(3).tolist(), "rotation_taught_deg": 0.0}}
    dets = _dets({1: (300.0, 0.0, 0.0), 2: (300.0, 70.0, 0.0)})    # container 2 right where well_1 pours
    with pytest.raises(V.VisionError, match="over container 2"):
        V.well_target("well_1", dets, offsets=offsets, cfg=CFG)
    with pytest.raises(V.VisionError, match="tube rack"):
        V.well_target("well_1", _dets({1: (300.0, 0.0, 0.0)}), offsets=offsets,
                      cfg={**CFG, "tube_rack_mm": {"min": [280, 40], "max": [320, 80]}})
    with pytest.raises(V.VisionError, match="workspace"):
        V.well_target("well_1", _dets({1: (390.0, 380.0, 0.0)}), offsets=offsets, cfg=CFG)
    with pytest.raises(V.VisionError, match="no taught offset"):
        V.well_target("well_3", dets, offsets=offsets, cfg=CFG)


def test_averaging_uses_a_circular_mean():
    a = _dets({1: (100.0, 0.0, 179.0)})
    b = _dets({1: (102.0, 2.0, -179.0)})
    m = V.average_detections([a, b], [1, 2, 3, 4])[1]
    assert (m.x_mm, m.y_mm) == (101.0, 1.0) and abs(V.wrap_deg(m.rotation_deg - 180.0)) < 1e-6


def test_saving_a_well_offset_keeps_the_taught_spots(tmp_path):
    import spots
    f = tmp_path / "spots.yaml"
    f.write_text((ROBOTICS / "spots.yaml").read_text())
    spots.save_spot("well_1", {i: float(i) for i in range(1, 7)}, f)
    V.save_well_offset("well_1", {"dx_mm": 1.0, "dy_mm": 2.0}, f)
    assert spots.get_spot("well_1", f)[3] == 3.0
    assert V.load_well_offsets(f)["well_1"]["dy_mm"] == 2.0
    spots.save_spot("slot_A", {i: 0.0 for i in range(1, 7)}, f)   # spots.py must not drop well_offsets
    assert V.load_well_offsets(f)["well_1"]["dx_mm"] == 1.0


def test_table_calibration_round_trips_through_the_file(table, tmp_path):
    f = tmp_path / "vision_calib.yaml"
    V.save_calib_section("table", table.to_dict(), f)
    t2 = V.Table.load(f)
    assert np.allclose(t2.H, table.H) and t2.scan_pose == SCAN_POSE and not t2.undistorted
    with pytest.raises(V.VisionError, match="not calibrated"):
        V.Table.load(tmp_path / "missing.yaml")


def test_markers_pdf_is_written(tmp_path):
    out = tmp_path / "markers.pdf"
    V.write_markers_pdf(out, CFG, dpi=100)
    assert out.read_bytes()[:4] == b"%PDF"
