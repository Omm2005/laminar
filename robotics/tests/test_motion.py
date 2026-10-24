"""Checks for robotics/motion.py. Pure math: no arm, no arm_server needed.

Run from robotics/:   python -m pytest -q tests
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import motion as M  # noqa: E402

# A real pose from teleop/kinematics.py (the demo grasp), used as "tube held in the stand".
GRASP = {1: -31.0, 2: 100.0, 3: 11.0, 4: 88.0, 5: 29.0, 6: -5.0}


def _angle_between(Ra, Rb):
    return math.degrees(np.linalg.norm(M._rot_log(Ra @ Rb.T)))


def test_rotation_helpers_round_trip():
    w = np.array([0.3, -0.2, 0.5])
    assert np.allclose(M._rot_log(M._rot_exp(w)), w, atol=1e-9)


def test_lift_goes_straight_up_and_keeps_orientation():
    p0, R0 = M.fk_mm(GRASP)
    path = M.plan_lift(GRASP, 100)
    for q in path.joints:
        p, R = M.fk_mm(q)
        assert np.linalg.norm(p[:2] - p0[:2]) < 1.0      # no sideways drift
        assert _angle_between(R, R0) < 2.0                 # tube attitude unchanged
    p_end, _ = M.fk_mm(path.final_joints)
    assert abs((p_end[2] - p0[2]) - 100) < 1.0
    zs = [M.fk_mm(q)[0][2] for q in path.joints]
    assert all(b >= a - 0.05 for a, b in zip(zs, zs[1:]))  # only ever goes up


def test_lower_after_lift_returns_to_start():
    up = M.plan_lift(GRASP, 60)
    down = M.plan_lower(up.final_joints, 60)
    p0, _ = M.fk_mm(GRASP)
    p1, _ = M.fk_mm(down.final_joints)
    assert np.linalg.norm(p1 - p0) < 1.0


def test_travel_stays_in_plane_on_a_straight_line():
    up = M.plan_lift(GRASP, 100)
    q_top = up.final_joints
    target = dict(q_top)
    target[1] += 20.0                                      # same pose, base turned 20 deg
    p_start, _ = M.fk_mm(q_top)
    p_tgt, _ = M.fk_mm(target)
    path = M.plan_travel(q_top, target)
    d = (p_tgt - p_start)[:2]
    d /= np.linalg.norm(d)
    for q in path.joints:
        p, _ = M.fk_mm(q)
        assert abs(p[2] - p_start[2]) < 1.0               # same height throughout
        off = p[:2] - p_start[:2]
        assert abs(off[0] * d[1] - off[1] * d[0]) < 1.0   # on the straight line
    p_end, _ = M.fk_mm(path.final_joints)
    assert np.linalg.norm(p_end[:2] - p_tgt[:2]) < 1.0


def test_joint_speeds_respect_the_cap():
    path = M.plan_lift(GRASP, 100, speed_mm_s=200)        # ask for something too fast
    J = np.array([[q[i] for i in range(1, 7)] for q in path.joints])
    rates = np.abs(np.diff(J, axis=0)) * M.CONTROL_HZ
    assert rates.max() <= M.MAX_JOINT_SPEED_DEG_S + 1e-6


def test_unreachable_move_is_refused():
    with pytest.raises(M.PlanError):
        M.plan_lift(GRASP, 2000)


def test_travel_refuses_a_target_that_would_tilt_the_tube():
    target = dict(GRASP)
    target[4] += 25.0                                      # wrist flex: tilts the tool
    with pytest.raises(M.PlanError):
        M.plan_travel(GRASP, target)


def test_workspace_box_is_enforced():
    p0, _ = M.fk_mm(GRASP)
    box = ([-1000, -1000, -1000], [1000, 1000, p0[2] + 50])  # ceiling 50 mm above the start
    with pytest.raises(M.PlanError):
        M.plan_lift(GRASP, 100, workspace_mm=box)
