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
        self.tilts, self.table, self.ended = [], None, False

    def start_table_tilt(self, table, step_deg, **kw):
        self.table = table

    def set_tilt(self, deg, done=False):
        self.tilts.append(deg)

    def end_tilt_stream(self):
        self.ended = True


def test_tip_tilt_joint_sends_the_table_once_then_only_the_angle():
    q, toward = _pour_start()
    table = PT.plan_tilt_table(q, toward, max_deg=20)
    c = FakeClient()
    j = PT.TipTiltJoint(c, table)
    assert j.position() == 0.0 and c.table is table
    j.move_to(math.radians(10), vlim=math.radians(60))
    time.sleep(0.6)
    assert abs(math.degrees(j.position()) - 10) < 0.5
    j.close()
    assert c.ended and c.tilts and abs(c.tilts[-1] - 10) < 0.5
    assert all(b >= a - 1e-9 for a, b in zip(c.tilts, c.tilts[1:]))   # the angle only ramps up to 10 here


def test_server_tilt_tracking_is_smooth_on_25_hz_updates():
    """arm_server.table_tilt's tracking (same math), fed a tilt ramp updated 25 times a second: the joints' speed
    must not step at each update (that shook the arm at 25 Hz)."""
    import numpy as np
    q, toward = _pour_start()
    table = PT.plan_tilt_table(q, toward, max_deg=60)
    n, dt, tau, acc = len(table) - 1, 0.01, 0.12, 400.0
    gain = [max(abs(table[i + 1][m] - table[i][m]) for m in range(1, 7)) for i in range(n)]
    tilt = v = goal = 0.0
    joints = []
    for k in range(600):
        if k % 4 == 0:
            goal = min(n, 15.0 * k * dt)
        i = min(int(tilt), n - 1)
        cap = min(60.0, 45.0 / max(gain[i], 1e-3))
        want = max(-cap, min(cap, (goal - tilt) / tau))
        v += max(-acc * dt, min(acc * dt, want - v))
        tilt = max(0.0, min(n, tilt + v * dt))
        joints.append([PT.joints_at(table, tilt)[m] for m in range(1, 7)])
    jv = np.diff(np.array(joints), axis=0) / dt
    assert np.max(np.abs(np.diff(jv, axis=0))) < 10.0          # deg/s change per 10 ms tick


def test_job_specs_and_targets():
    from types import SimpleNamespace
    import pour_episode as P
    assert P.parse_spec("A2b:50", 0.9) == ("A2b", "abs", 0.5)
    assert P.parse_spec("B2b:+50", 0.9) == ("B2b", "add", 0.5)
    assert P.parse_spec("C2c", 0.9) == ("C2c", "abs", 0.9)

    class Tr:
        columns = {"B": SimpleNamespace(roi=(0, 0, 10, 200))}

        def read_stable(self, name):
            return SimpleNamespace(px=100.0)            # half full

    w = SimpleNamespace(tracker=Tr())
    assert P.resolve_target("abs", 0.7, w, "B") == (0.7, 0.5)
    assert P.resolve_target("add", 0.3, w, "B")[0] == 0.8
    assert P.resolve_target("add", 0.5, w, "B")[0] == P.TARGET_CAP   # 100 % is capped
