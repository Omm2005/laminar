"""Shared bits for all pour/primitives/*.

Sets up sys.path so each primitive can `import arm_io` (pour/) and `import kinematics` (teleop/),
loads the three YAML config files once, and exposes helpers for pose preconditions, dry-run
printing, and FK-based waypoint moves.
"""
import sys
from pathlib import Path

import numpy as np
import yaml

PRIM_DIR = Path(__file__).resolve().parent
POUR_DIR = PRIM_DIR.parent
LAMINAR = POUR_DIR.parent
# post-merge: kinematics now lives in robotics/ (used to be teleop/).
TELEOP = LAMINAR / "robotics"
for p in (str(POUR_DIR), str(TELEOP)):
    if p not in sys.path:
        sys.path.insert(0, p)

import arm_io
import kinematics as K


def _load(name):
    return yaml.safe_load((POUR_DIR / "data" / name).read_text())


POSES = _load("poses.yaml") or {}
POUR = _load("pour.yaml") or {}
CHANNELS = (_load("channels.yaml") or {}).get("channels", {})

NEAR_POSE_TOL_DEG = 10.0
Z_DIP_TOL_M = 0.020


def pose_joints(name):
    p = POSES.get(name)
    if not p:
        raise SystemExit(f"pose {name!r} not in poses.yaml (defined: {sorted(POSES)})")
    return {m: float(p[f"J{m}"]) for m in range(1, 7)}


def pose_gripper(name):
    g = (POSES.get(name) or {}).get("gripper")
    return None if g is None else float(g)


def require_near(expected, current, tol_deg=NEAR_POSE_TOL_DEG):
    """Refuse if any joint diffs > tol_deg from expected. Raises SystemExit with details."""
    worst_m, worst_d = max(((m, current[m] - expected[m]) for m in current), key=lambda t: abs(t[1]))
    if abs(worst_d) > tol_deg:
        diff_str = "  ".join(f"J{m}={current[m] - expected[m]:+.1f}" for m in sorted(current))
        raise SystemExit(f"refusing: current pose differs from expected by J{worst_m}={worst_d:+.1f} "
                         f"(limit {tol_deg} deg).  Diffs: {diff_str}")


def plan_line(label, joints):
    p, R = K.fk(joints)
    jstr = "  ".join(f"J{m}={joints[m]:+6.1f}" for m in range(1, 7))
    return f"{label:16s} {jstr}  | xyz={np.round(p, 3).tolist()}"


def send_move(target, speed, dry_run, label=""):
    """Clamp, print the planned joint targets + FK xyz, send unless dry_run."""
    target = arm_io.clamp_joints(target)
    print(plan_line(label, {m: target[m] for m in range(1, 7)}))
    if dry_run:
        return None
    return arm_io.send_cmd("move",
                           joints={str(m): round(target[m], 2) for m in range(1, 7)},
                           speed=speed, timeout=120.0)


def waypoints_xyz(start_joints, dx, dy, dz, steps, speed, dry_run, label):
    """Straight-line in base-frame xyz, orientation held, split into `steps` waypoints via IK.
    Returns the final joint dict (planned or actual)."""
    p0, R = K.fk(start_joints)
    q = dict(start_joints)
    for k in range(1, steps + 1):
        frac = k / steps
        p_target = p0 + np.array([dx * frac, dy * frac, dz * frac])
        q_new, err = K.ik(q, p_target, R, limits=arm_io.LIMITS)
        if err > 0.003:
            raise SystemExit(f"IK failed at waypoint {k}/{steps}: err {err*1000:.1f} mm")
        send_move(q_new, speed=speed, dry_run=dry_run, label=f"{label} {k}/{steps}")
        q = q_new
    return q


def check_no_dip(start_joints, target_joints, steps=5, tol_m=Z_DIP_TOL_M):
    """Interpolate joint-space between start and target; abort if intermediate FK Z drops
    more than tol_m below min(start.z, target.z). Used by traverse and return_vial."""
    pz_start = K.fk(start_joints)[0][2]
    pz_target = K.fk(target_joints)[0][2]
    floor = min(pz_start, pz_target) - tol_m
    for k in range(1, steps):
        frac = k / steps
        q = {m: start_joints[m] + (target_joints[m] - start_joints[m]) * frac for m in range(1, 7)}
        if K.fk(q)[0][2] < floor:
            raise SystemExit(f"traverse would dip below {floor*1000:.0f} mm at waypoint {k}/{steps}")
