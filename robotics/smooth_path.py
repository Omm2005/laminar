"""Smooth every joint path arm_server plays (teleop recordings above all) before it moves the arm.

The recordings are 20 Hz human motion: tremor, 0.1 deg steps, and arm_server joins the points with straight
lines, so the commanded velocity jumps every 50 ms (that is what the jerk column on the Runs page shows).
smooth() runs a zero-phase Gaussian filter over the arm joints and resamples at OUT_HZ with a cubic, so the
command is continuous in velocity. The first and last points stay exactly where they were (the path must
start at the current pose, and the last point is the grasp); the gripper channel is not filtered (its close
timing is part of the grasp), only resampled.

    python smooth_path.py vial_B          # how much it changes the path: max deviation, jerk before/after
"""
from __future__ import annotations

import argparse

import numpy as np

SIGMA_S = 0.15   # filter width: removes hand tremor above ~2 Hz, keeps the deliberate motion
OUT_HZ = 50.0
ARM = range(1, 7)
GRIPPER = 7


def _gauss_filter(x: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing along axis 0, edges padded with the end values (zero phase: no lag)."""
    r = max(1, int(round(3 * sigma)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.concatenate([np.repeat(x[:1], r, axis=0), x, np.repeat(x[-1:], r, axis=0)])
    return np.stack([np.convolve(pad[:, j], k, mode="valid") for j in range(x.shape[1])], axis=1)


def _cubic(t_in: np.ndarray, y: np.ndarray, t_out: np.ndarray) -> np.ndarray:
    """Catmull-Rom cubic through (t_in, y) evaluated at t_out (uniform t_in), per column."""
    h = t_in[1] - t_in[0]
    u = np.clip((t_out - t_in[0]) / h, 0, len(t_in) - 1)
    i = np.minimum(u.astype(int), len(t_in) - 2)
    f = (u - i)[:, None]
    p0, p1 = y[np.maximum(i - 1, 0)], y[i]
    p2, p3 = y[i + 1], y[np.minimum(i + 2, len(y) - 1)]
    return 0.5 * ((2 * p1) + (-p0 + p2) * f + (2 * p0 - 5 * p1 + 4 * p2 - p3) * f ** 2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * f ** 3)


def smooth(points: list[dict[int, float]], hz: float, sigma_s: float = SIGMA_S,
           out_hz: float = OUT_HZ) -> tuple[list[dict[int, float]], float]:
    """(points, hz) -> (smoothed points at out_hz, out_hz). Same duration, same first and last point."""
    if len(points) < 4:
        return points, hz
    arm = np.array([[p[j] for j in ARM] for p in points], dtype=float)
    sm = _gauss_filter(arm, sigma_s * hz)
    # pin both ends: blend the filter's end offsets back out over the first / last sigma-wide stretch
    n = len(sm)
    w = np.clip(np.arange(n) / max(1.0, 2 * sigma_s * hz), 0, 1)[:, None]
    sm = sm + (arm[0] - sm[0]) * (1 - w) + (arm[-1] - sm[-1]) * (1 - w[::-1])
    t_in = np.arange(n) / hz
    steps = max(1, round(t_in[-1] * out_hz))
    out_hz = steps / t_in[-1]  # evenly spaced and exactly as long as the recording
    t_out = np.linspace(0.0, t_in[-1], steps + 1)
    arm_out = _cubic(t_in, sm, t_out)
    arm_out[0], arm_out[-1] = arm[0], arm[-1]
    out = [{j: float(v) for j, v in zip(ARM, row)} for row in arm_out]
    if all(GRIPPER in p for p in points):
        g = np.interp(t_out, t_in, [p[GRIPPER] for p in points])
        for p, v in zip(out, g):
            p[GRIPPER] = float(v)
    return out, out_hz


def max_deviation(raw: list[dict[int, float]], hz: float, sm: list[dict[int, float]], shz: float) -> float:
    """Largest difference (deg, any arm joint) between a path and its smoothed version, at the raw times."""
    t_in = np.arange(len(raw)) / hz
    t_sm = np.arange(len(sm)) / shz
    return max(float(np.abs(np.interp(t_in, t_sm, [p[j] for p in sm]) - [p[j] for p in raw]).max()) for j in ARM)


def jerk_rms(points: list[dict[int, float]], hz: float, step_hz: float = 100.0) -> float:
    """RMS jerk (deg/s^3) of the command arm_server actually plays: linear between points, sampled at step_hz."""
    arm = np.array([[p[j] for j in ARM] for p in points], dtype=float)
    t_in = np.arange(len(arm)) / hz
    t = np.arange(0, t_in[-1], 1 / step_hz)
    q = np.stack([np.interp(t, t_in, arm[:, j]) for j in range(6)], axis=1)
    j = np.diff(q, n=3, axis=0) * step_hz ** 3
    return float(np.sqrt((j ** 2).mean()))


def main() -> None:
    from paths import load
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--sigma", type=float, default=SIGMA_S)
    a = ap.parse_args()
    m = load(a.name)
    pts, hz = m["points"], m["hz"]
    sm, shz = smooth(pts, hz, a.sigma)
    dev = max_deviation(pts, hz, sm, shz)
    print(f"{a.name}: {len(pts)} pts @ {hz:g} Hz -> {len(sm)} pts @ {shz:g} Hz, {(len(pts) - 1) / hz:.1f} s")
    print(f"  max deviation from the recording {dev:.2f} deg; end point unchanged: "
          f"{all(abs(sm[-1][j] - pts[-1][j]) < 1e-9 for j in ARM)}")
    print(f"  commanded RMS jerk {jerk_rms(pts, hz):,.0f} -> {jerk_rms(sm, shz):,.0f} deg/s^3")


if __name__ == "__main__":
    main()
