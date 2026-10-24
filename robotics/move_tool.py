"""Move the follower's tool in a straight-ish line via IK (through arm_server).
Usage: python move_tool.py --fwd 0.03 [--left 0] [--up 0] [--speed 8]
fwd/left are along / across the tool's current horizontal approach direction; up is vertical (m)."""
import argparse, json, math, subprocess, sys
import numpy as np
import kinematics as K
from arm_server import LIMITS, STATUS_FILE

ap = argparse.ArgumentParser()
ap.add_argument("--fwd", type=float, default=0.0)
ap.add_argument("--left", type=float, default=0.0)
ap.add_argument("--up", type=float, default=0.0)
ap.add_argument("--speed", type=float, default=8.0)
ap.add_argument("--steps", type=int, default=4, help="split into waypoints so the joint-space path stays near the line")
a = ap.parse_args()
st = json.loads(STATUS_FILE.read_text())
q = {i: st["command"][f"J{i}"] for i in range(1, 7)}
p, R = K.fk(q)
p6, _ = None, None
# horizontal approach direction = tool position minus the joint-6 origin, flattened
T = np.eye(4); pts = []; i = 0
for n, jt, T0, ax in K.CHAIN:
    T = T @ T0
    if jt == "revolute":
        i += 1; Rj = np.eye(4); Rj[:3, :3] = K._axis_angle(ax, np.radians(q[i])); T = T @ Rj
    pts.append(T[:3, 3].copy())
d = pts[-1] - pts[-2]; d[2] = 0; d /= np.linalg.norm(d)
left = np.array([-d[1], d[0], 0.0])
delta = a.fwd * d + a.left * left + np.array([0, 0, a.up])
lim = {m: LIMITS[m] for m in range(1, 7)}
for k in range(1, a.steps + 1):
    q, err = K.ik(q, p + delta * k / a.steps, R, lim)
    if err > 0.003:
        sys.exit(f"IK failed (err {err*1000:.1f} mm) at step {k}")
    args = ["python", "arm_cmd.py", "move"] + [f"{m}={q[m]:.2f}" for m in range(1, 7)] + ["--speed", str(a.speed)]
    out = json.loads(subprocess.run(args, capture_output=True, text=True).stdout)
pn, _ = K.fk({m: out["state"][f"J{m}"] for m in range(1, 7)})
print(out["result"], "| state", out["state"], "| tool", np.round(pn, 3))
