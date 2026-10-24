"""Checks for the vial -> compartment round trip (motion.plan_transfer / plan_return) and transfer.py.
Pure math plus a simulated pour (pour_motor6, ~20 s real time): no arm, no arm_server, no camera needed.

Run from robotics/:   python -m pytest -q tests
"""
import math
import time
import sys
from pathlib import Path

import numpy as np
import pytest

ROBOTICS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROBOTICS))
import motion as M  # noqa: E402
import transfer as T  # noqa: E402

GRASP = {1: -31.0, 2: 100.0, 3: 11.0, 4: 88.0, 5: 29.0, 6: -5.0}  # real demo grasp pose (tube in stand)
DXY = (40.0, -120.0)


def _angle_between(Ra, Rb):
    return math.degrees(np.linalg.norm(M._rot_log(Ra @ Rb.T)))


def test_shift_is_a_flat_pure_translation():
    q_top = M.plan_lift(GRASP, 100).final_joints
    p0, R0 = M.fk_mm(q_top)
    path = M.plan_shift(q_top, DXY)
    d = np.array(DXY) / np.linalg.norm(DXY)
    for q in path.joints:
        p, R = M.fk_mm(q)
        assert abs(p[2] - p0[2]) < 1.0                      # height locked
        assert _angle_between(R, R0) < 2.0                   # orientation locked: tube stays upright
        off = p[:2] - p0[:2]
        assert abs(off[0] * d[1] - off[1] * d[0]) < 1.0      # on the straight line
    p1, _ = M.fk_mm(path.final_joints)
    assert np.allclose(p1[:2] - p0[:2], DXY, atol=1.0)


def test_transfer_heights_and_offsets():
    lift, shift, lower = M.plan_transfer(GRASP, DXY)
    p0, _ = M.fk_mm(GRASP)
    p_pour, _ = M.fk_mm(lower.final_joints)
    assert np.allclose(p_pour - p0, [DXY[0], DXY[1], 100.0 - 60.0], atol=1.0)  # 40 mm above the grab height
    assert lift.label.startswith("lift 100") and lower.label.startswith("lower 60")


def test_return_reverses_every_parameter_and_lands_on_the_grab_pose():
    out = M.plan_transfer(GRASP, DXY)
    up, back, down = M.plan_return(out[-1].final_joints, DXY)
    assert up.label.startswith("lift 60") and down.label.startswith("lower 100")
    p_top_out, _ = M.fk_mm(out[1].final_joints)
    p_top_back, _ = M.fk_mm(up.final_joints)
    assert abs(p_top_out[2] - p_top_back[2]) < 1.0        # same carry plane both ways
    p0, R0 = M.fk_mm(GRASP)
    p1, R1 = M.fk_mm(down.final_joints)
    assert np.linalg.norm(p1 - p0) < 1.0 and _angle_between(R0, R1) < 1.0


def test_whole_round_trip_is_continuous():
    paths = M.plan_transfer(GRASP, DXY)
    paths += M.plan_return(paths[-1].final_joints, DXY)
    for a, b in zip(paths, paths[1:]):
        assert max(abs(a.final_joints[j] - b.joints[0][j]) for j in range(1, 7)) < 1e-6


def test_layout_route_errors_are_clear(tmp_path):
    f = tmp_path / "layout.yaml"
    f.write_text("routes:\n  vial_A: {well_1: [10, -20], well_2: null}\n")
    lay = T.load_layout(f)
    assert T.route(lay, "vial_A", "well_1") == (10.0, -20.0)
    with pytest.raises(ValueError):
        T.route(lay, "vial_A", "well_2")      # not measured yet
    with pytest.raises(KeyError):
        T.route(lay, "vial_Z", "well_1")


def test_shipped_layout_parses():
    lay = T.load_layout()
    assert lay["lift_mm"] == 100 and lay["lower_mm"] == 60
    assert set(lay["routes"]) == {"vial_A", "vial_B", "vial_C", "vial_D"}


def test_roll_room_respects_the_wrist_limit():
    lo, hi = M.JOINT_LIMITS_DEG[6]
    assert T.roll_room_deg(-5.0, +1) == pytest.approx(hi - (-5.0) - T.ROLL_MARGIN_DEG)
    assert T.roll_room_deg(-5.0, -1) == pytest.approx(-5.0 - lo - T.ROLL_MARGIN_DEG)
    assert T.roll_room_deg(hi, +1) == 0.0


def test_pour_direction_and_camera_column():
    cfg = {"direction": -1, "columns": {"well_2": "B"}}
    assert T.pour_direction(cfg) == -1 and T.pour_direction(cfg, "right") == 1
    assert T.pour_direction({}) == 1
    assert T.camera_column(cfg, "well_2") == "B" and T.camera_column(cfg, "A") == "A"
    lay = T.load_layout()
    assert T.camera_column(lay["pour"], "well_1") == "A"


def test_camera_pour_refuses_before_opening_the_camera_when_not_calibrated(tmp_path):
    with pytest.raises(RuntimeError, match="not calibrated"):
        T.CameraPour("0", tmp_path / "missing.json", "A")


def test_pour_motor6_runs_on_the_tilt_stream_and_ends_upright(monkeypatch):
    """Om's pour_right through ServerTiltJoint (arm_server's tilt_stream), simulated liquid and camera."""
    from laminar import pour_motor6 as P6

    tilts = []

    class FakeClient:
        def current_joints(self):
            return {j: 0.0 for j in range(1, 7)}

        def start_tilt_stream(self, joint=6, max_rate_deg_s=120.0, timeout_s=0.5):
            return -5.0

        def set_tilt(self, deg, done=False):
            tilts.append(deg)

        def end_tilt_stream(self):
            tilts.append(0.0)

    import arm_client
    monkeypatch.setattr(arm_client, "ArmClient", FakeClient)
    joint = P6.ServerTiltJoint(6)
    try:
        r = P6.motor_pour(joint, +1, target_frac=0.9, verbose=False)   # real time, simulated liquid
    finally:
        joint.close()
    assert r.ok, r.reason
    assert max(tilts) <= 90 - (-5.0)                # never past the wrist limit
    assert tilts[-1] == 0.0                         # back upright


def _camera_pour(tmp_path, px, conf=1.0):
    """A CameraPour without a camera: fake percent calibration (4 mL full, 200 px box) + a stub tracker."""
    import types
    from laminar import pour_motor6 as P6
    from level import LevelSample
    cp = T.CameraPour.__new__(T.CameraPour)
    cp.P6, cp.column, cp.empty = P6, "A", []
    cp.calib = str(P6.fake_calibrate(tmp_path / "calib.json"))
    cp.tracker = types.SimpleNamespace(read=lambda name: LevelSample(px * 0.02, px, conf, time.time()))
    cp.asked = []

    def fake_pour(direction, target, max_tilt, view=True, on_tick=None):
        cp.asked.append(target)
        return types.SimpleNamespace(ok=True, final_ml=(target * 4.0), result="done", reason="target reached")
    cp.pour = fake_pour
    return cp


def test_add_ml_pours_on_top_of_what_the_channel_holds(tmp_path):
    cp = _camera_pour(tmp_path, px=50)                  # 25 % of a 4 mL channel = 1 mL already in it
    assert cp.level() == pytest.approx((0.25, 1.0, 4.0))
    r, added = cp.pour_ml(+1, 2.0, 85.0, view=False)
    assert cp.asked == [pytest.approx(0.75)]            # 1 mL + 2 mL = 75 % of full, not 50 % of full
    assert added == pytest.approx(2.0)


def test_add_ml_refuses_to_overfill_without_tilting(tmp_path):
    cp = _camera_pour(tmp_path, px=150)                 # 3 mL of 4
    with pytest.raises(RuntimeError, match="overflow"):
        cp.pour_ml(+1, 2.0, 85.0, view=False)
    assert cp.asked == []


def test_add_ml_refuses_when_the_camera_cannot_read_the_column(tmp_path):
    cp = _camera_pour(tmp_path, px=0, conf=0.1)
    with pytest.raises(RuntimeError, match="can't read column"):
        cp.pour_ml(+1, 1.0, 85.0, view=False)
