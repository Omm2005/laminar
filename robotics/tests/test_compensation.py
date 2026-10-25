import numpy as np

from compensation import MAX_FF_NM, Compensation, fit_samples
from dynamics import gravity_torque
from smooth_path import jerk_rms, smooth


def test_smooth_keeps_the_ends_and_cuts_the_jerk():
    rng = np.random.default_rng(0)
    t = np.arange(200) / 20.0
    pts = [{j: 30 * np.sin(0.3 * tt + j) + rng.normal(0, 0.3) for j in range(1, 7)} | {7: 0.0 if tt < 5 else 20.0}
           for tt in t]
    out, hz = smooth(pts, 20.0)
    assert abs(hz - 50.0) < 0.5 and abs((len(out) - 1) / hz - t[-1]) < 1e-6
    assert out[0] == {**pts[0], 7: pts[0][7]} and all(out[-1][j] == pts[-1][j] for j in range(1, 7))
    assert jerk_rms(out, hz) < jerk_rms(pts, 20.0) / 3


def test_fit_finds_friction_and_trusts_gravity_only_where_it_fits():
    rng = np.random.default_rng(1)
    n = 600
    ts = np.arange(n) * 0.2
    cmd = np.zeros((n, 6))
    cmd[:, 1] = 60 + 10 * ((np.arange(n) // 25) % 8)                # J2 steps and holds: gravity load varies
    cmd[:, 0] = np.where((np.arange(n) // 50) % 3 == 0, 0, np.cumsum(np.sign(np.sin(np.arange(n) / 30))))
    cmd[:, 2] = 40.0
    q = cmd.copy()
    cv = np.vstack([np.zeros(6), np.diff(cmd, axis=0) / 0.2])
    g = np.array([[gravity_torque({m: float(v) for m, v in enumerate(row, 1)})[m] for m in range(1, 7)] for row in q])
    tau = 0.9 * g + rng.normal(0, 0.05, g.shape)
    tau[:, 0] += 0.5 * np.sign(cv[:, 0])                             # J1: 0.5 N.m Coulomb friction
    fit = fit_samples(ts, q, tau, cmd)
    assert abs(fit["friction_nm"][1] - 0.5) < 0.05
    assert abs(fit["gravity_scale"][2] - 0.9) < 0.05 and 1 not in fit["gravity_scale"]


def test_feedforward_is_clamped_and_follows_motion_direction():
    c = Compensation({2: 1.0}, {1: 5.0}, clock=lambda: 0.0)
    ff = c.feedforward({m: 0.0 for m in range(1, 7)} | {2: 90.0}, {m: 0.0 for m in range(1, 7)} | {1: 1.0})
    assert ff[1] == MAX_FF_NM[1]                                     # 0.8 * 5 N.m clamped to the joint's limit
    assert abs(ff[2] - max(-MAX_FF_NM[2], min(MAX_FF_NM[2], gravity_torque({m: 0.0 for m in range(1, 7)} | {2: 90.0})[2]))) < 1e-9
    assert c.feedforward({m: 0.0 for m in range(1, 7)}, {m: 0.0 for m in range(1, 7)} | {1: -1.0})[1] < 0
