import math
import time

import numpy as np

import deliver as D
import motion as M
import pour_tilt as PT


def _pour_start():
    """Container b's pour pose (taught joints, EXTRA_LOWER_MM lower), and 'into the tray' for a tray at 3 deg."""
    offs = D.load_offsets()
    q0 = {int(k): float(v) for k, v in offs[2]["joints"].items()}
    p, R = M.fk_mm(q0)
    p = p - [0.0, 0.0, D.EXTRA_LOWER_MM]
    _, q = D.reachable_pour(p, R, q0)
    return q, D.into_tray({"xy": np.array([421.0, 287.0]), "heading_deg": 3.2})


def test_tilt_table_keeps_the_tip_locked_and_tips_towards_the_tray():
    q, toward = _pour_start()
    table = PT.plan_tilt_table(q, toward, max_deg=60)
    p0, R0 = M.fk_mm(table[0])
    assert len(table) > 30
    for k in range(0, len(table), 5):
        p, R = M.fk_mm(table[k])
        assert np.linalg.norm(p - p0) < 1.5                         # the tip does not move
    _, Rk = M.fk_mm(table[30])
    assert float((Rk[:, 2] - R0[:, 2])[:2] @ toward) > 0             # the vial's top went towards the tray


def test_best_start_reaches_past_horizontal():
    q, toward = _pour_start()
    g, q_start, table = PT.best_pour_start(q, toward)
    assert (len(table) - 1) * PT.TILT_STEP_DEG >= 110                # J6 alone stops near 90
    p, _ = M.fk_mm(q)
    assert np.linalg.norm(M.fk_mm(q_start)[0] - p) < 1.5             # the heading turn keeps the tip in place


def test_joints_at_interpolates_between_samples():
    table = [{j: 0.0 for j in range(1, 7)}, {j: 1.0 for j in range(1, 7)}, {j: 3.0 for j in range(1, 7)}]
    assert PT.joints_at(table, 1.5)[4] == 2.0 and PT.joints_at(table, 99)[1] == 3.0 and PT.joints_at(table, -5)[2] == 0.0


class FakeClient:
    def __init__(self):
        self.poses, self.started, self.ended = [], False, False

    def start_pose_stream(self, **kw):
        self.started = True

    def set_pose(self, joints, done=False):
        self.poses.append(dict(joints))

    def end_pose_stream(self):
        self.ended = True


def test_tip_tilt_joint_streams_table_joints_and_ends_the_stream():
    q, toward = _pour_start()
    table = PT.plan_tilt_table(q, toward, max_deg=20)
    c = FakeClient()
    j = PT.TipTiltJoint(c, table)
    assert j.position() == 0.0 and c.started
    j.move_to(math.radians(10), vlim=math.radians(60))
    time.sleep(0.6)
    assert abs(math.degrees(j.position()) - 10) < 0.5
    j.close()
    assert c.ended and c.poses
    p0, _ = M.fk_mm(table[0])
    assert np.linalg.norm(M.fk_mm(c.poses[-1])[0] - p0) < 1.5
