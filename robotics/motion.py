"""Straight-line moves for the reBot B601-RS with the tube held upright.

This module plans the three shared moves that every Laminar task uses:

    plan_lift(q, distance_mm)       straight UP from wherever the gripper is now
    plan_lower(q, distance_mm)      straight DOWN from wherever the gripper is now
    plan_travel(q, target)          straight line in the SAME horizontal plane to a taught spot
    plan_shift(q, (dx, dy))         same plane, pure translation by a 2-D offset (height + orientation locked)
    plan_transfer / plan_return     up 100 -> shift -> down 60, and the exact reverse

Each one returns a `Path`: a time-stamped list of joint angles the arm should follow.
Planning never moves the arm. To actually move, hand the Path to `arm_client.ArmClient.run(path)`.

How a plan is built (same for all three moves):
    1. Forward kinematics (FK) of the current joints gives the gripper's start position and orientation.
    2. We decide the end position (and end orientation for travel).
    3. We cut the straight line between them into small pieces, 1 mm apart by default.
    4. For each piece we solve inverse kinematics (IK): "put the gripper here, oriented like this"
       -> joint angles. Each solve starts from the previous answer so the arm never flips into a
       different elbow/wrist configuration halfway through.
    5. We check every piece: IK converged, joint limits respected, no sudden joint jumps,
       tube orientation held, gripper inside the allowed workspace box.
       If ANY check fails we raise PlanError and nothing moves.
    6. We give the motion a smooth speed profile (minimum-jerk: gentle start, gentle stop) so
       the liquid doesn't slosh, and sample it at the control rate (default 50 Hz).

Units used everywhere in this file's public functions:
    joint angles  -> degrees, as a dict {1: J1, 2: J2, ..., 6: J6}
                     (the follower motor frame: same numbers arm_server.py and the
                     recorder's data.csv use; 0 = calibrated zero pose)
    positions     -> millimetres, in the arm base frame (x forward, y left, z up)
    speeds        -> mm/s for the gripper, deg/s for joints
    time          -> seconds

The gripper (joint 7) is never touched by this module.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path as FilePath

import numpy as np

# ---------------------------------------------------------------------------------------------
# Reuse the FK in robotics/kinematics.py (already validated on the real arm with teleop), so
# every script agrees on what "joint angles" and "tool position" mean. It only needs numpy.
# ---------------------------------------------------------------------------------------------
sys.path.insert(0, str(FilePath(__file__).resolve().parent))
import kinematics as K  # noqa: E402  (robotics/kinematics.py: fk(q_deg) -> (xyz in m, 3x3 rotation))

# ---------------------------------------------------------------------------------------------
# Tunable settings. Every default here is a starting guess; tune on the real arm.
# ---------------------------------------------------------------------------------------------

# Follower joint limits in degrees from the zero pose. Copied from leader_arm.py MAPPING
# (importing that file would pull in the leader-arm serial driver, which we don't need here).
JOINT_LIMITS_DEG: dict[int, tuple[float, float]] = {
    1: (-145.0, 145.0),  # shoulder_pan
    2: (0.0, 170.0),     # shoulder_lift
    3: (0.0, 180.0),     # elbow_flex
    4: (-80.0, 90.0),    # wrist_flex
    5: (-90.0, 90.0),    # wrist_yaw
    6: (-90.0, 90.0),    # wrist_roll (the pour joint)
}

CONTROL_HZ = 50.0             # how many joint targets per second the Path contains
SAMPLE_SPACING_MM = 1.0       # distance between IK solves along the line
DEFAULT_LIFT_SPEED = 20.0     # mm/s peak gripper speed going up
DEFAULT_LOWER_SPEED = 15.0    # mm/s peak gripper speed going down (slower: we are near glass)
DEFAULT_TRAVEL_SPEED = 30.0   # mm/s peak gripper speed sideways (slosh is the limit here)
MAX_JOINT_SPEED_DEG_S = 45.0  # if a path needs any joint faster than this, the path is slowed down
MAX_JUMP_PER_MM_DEG = 3.0     # any joint changing more than this between two 1 mm samples = a flip
POS_TOL_MM = 1.0              # IK must land within this distance of each sample
ANG_TOL_DEG = 2.0             # ... and within this angle of the wanted orientation
MAX_TILT_CHANGE_DEG = 3.0     # travel start/end orientations may differ by a turn about vertical
                              # only; a difference that would tilt the tube more than this is refused
ORIENT_WEIGHT_M_PER_RAD = 0.2  # IK trade-off: 1 rad of orientation error "costs" like 0.2 m of position


class PlanError(Exception):
    """A move could not be planned safely. The message says which sample failed and why.
    Nothing has moved when this is raised."""


@dataclass
class Path:
    """A planned move, ready to send to the arm.

    times_s   : time of each target, starting at 0 (seconds), spaced 1/CONTROL_HZ apart
    joints    : joint targets, one dict {1..6: degrees} per time
    label     : short human description, e.g. "lift 100 mm"
    start_mm / end_mm : gripper position at the start / end (mm, base frame)
    max_pos_err_mm / max_ang_err_deg : worst IK error along the whole line (for logs / dashboards)
    notes     : warnings worth showing a human (e.g. "slowed to 18 mm/s near a wrist limit")
    """
    times_s: list[float]
    joints: list[dict[int, float]]
    label: str
    start_mm: np.ndarray
    end_mm: np.ndarray
    max_pos_err_mm: float = 0.0
    max_ang_err_deg: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.times_s[-1] if self.times_s else 0.0

    @property
    def final_joints(self) -> dict[int, float]:
        return self.joints[-1]

    def summary(self) -> str:
        """One line for the console / dashboard."""
        s, e = np.round(self.start_mm, 1), np.round(self.end_mm, 1)
        txt = (f"{self.label}: {s} -> {e} mm in {self.duration_s:.1f} s, "
               f"{len(self.joints)} targets, worst IK error {self.max_pos_err_mm:.2f} mm / "
               f"{self.max_ang_err_deg:.2f} deg")
        return txt + ("" if not self.notes else " | " + "; ".join(self.notes))


# =============================================================================================
# Small math helpers
# =============================================================================================

def _as_dict(q) -> dict[int, float]:
    """Accept joints as a dict {1..6: deg} or a 6-long list/array and return a clean dict."""
    if isinstance(q, dict):
        return {i: float(q[i]) for i in range(1, 7)}
    q = list(q)
    if len(q) != 6:
        raise ValueError(f"expected 6 joint angles, got {len(q)}")
    return {i + 1: float(q[i]) for i in range(6)}


def _vec(q: dict[int, float]) -> np.ndarray:
    return np.array([q[i] for i in range(1, 7)], dtype=float)


def _dict(v: np.ndarray) -> dict[int, float]:
    return {i + 1: float(v[i]) for i in range(6)}


def fk_mm(q) -> tuple[np.ndarray, np.ndarray]:
    """Forward kinematics.
    Input : q = joint angles in degrees (dict {1..6} or list of 6).
    Output: (position of the gripper tip in mm, 3x3 rotation matrix of the gripper) in the base frame.
    """
    p_m, R = K.fk(_as_dict(q))
    return np.asarray(p_m) * 1000.0, np.asarray(R)


def _rot_exp(w: np.ndarray) -> np.ndarray:
    """Rotation vector (axis * angle, rad) -> 3x3 rotation matrix (Rodrigues' formula)."""
    a = float(np.linalg.norm(w))
    if a < 1e-12:
        return np.eye(3)
    k = w / a
    Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(a) * Kx + (1 - math.cos(a)) * Kx @ Kx


def _rot_log(R: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix -> rotation vector (axis * angle, rad). Inverse of _rot_exp."""
    c = max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))
    a = math.acos(c)
    if a < 1e-9:
        return np.zeros(3)
    if a > math.pi - 1e-6:  # 180 degree turn: axis from the symmetric part
        M = (R + np.eye(3)) / 2.0
        k = np.sqrt(np.clip(np.diag(M), 0, None))
        k = k / (np.linalg.norm(k) or 1.0)
        return k * a
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    return v / (2.0 * math.sin(a)) * a


def _slerp(R0: np.ndarray, R1: np.ndarray, s: float) -> np.ndarray:
    """Orientation a fraction s (0..1) of the way from R0 to R1, turning about one fixed axis."""
    return _rot_exp(s * _rot_log(R1 @ R0.T)) @ R0


def _min_jerk(u: float) -> float:
    """Minimum-jerk profile: fraction of the distance covered at fraction u (0..1) of the time.
    Starts and ends with zero speed AND zero acceleration, which is what keeps liquid calm.
    Its peak speed is 1.875 x the average speed."""
    u = min(1.0, max(0.0, u))
    return u ** 3 * (10 - 15 * u + 6 * u * u)


# =============================================================================================
# Inverse kinematics
# =============================================================================================

def ik(q_seed, p_goal_mm: np.ndarray, R_goal: np.ndarray, iters: int = 100):
    """Solve for joint angles that put the gripper at p_goal_mm with orientation R_goal.

    Inputs
        q_seed    : starting guess, degrees (dict or list of 6). Use the previous sample's answer so
                    consecutive solutions stay on the same "branch" of the arm (no elbow flips).
        p_goal_mm : wanted gripper position, mm, base frame (3 numbers)
        R_goal    : wanted gripper orientation, 3x3 rotation matrix, base frame
        iters     : maximum solver iterations

    Output
        (q, pos_err_mm, ang_err_deg): joint dict in degrees, and how far the answer is from the goal.

    Method: damped least squares on a numerical Jacobian, with joint limits clamped each step.
    The caller decides whether the remaining error is acceptable.
    """
    q = _vec(_as_dict(q_seed))
    lo = np.array([JOINT_LIMITS_DEG[i][0] for i in range(1, 7)])
    hi = np.array([JOINT_LIMITS_DEG[i][1] for i in range(1, 7)])
    p_goal_m = np.asarray(p_goal_mm, dtype=float) / 1000.0

    def error(qv):
        # 6-number error: position (m) then orientation (rotation vector, rad, weighted)
        p, R = K.fk(_dict(qv))
        return np.concatenate([p_goal_m - np.asarray(p), ORIENT_WEIGHT_M_PER_RAD * _rot_log(R_goal @ np.asarray(R).T)])

    e = error(q)
    for _ in range(iters):
        if np.linalg.norm(e[:3]) < 5e-5 and np.linalg.norm(e[3:]) / ORIENT_WEIGHT_M_PER_RAD < 3e-4:
            break
        # Numerical Jacobian: how the error changes when each joint moves a tiny bit (per radian).
        J = np.zeros((6, 6))
        h_deg = 0.01
        for i in range(6):
            dq = q.copy()
            dq[i] += h_deg
            J[:, i] = -(error(dq) - e) / math.radians(h_deg)
        # Damped least squares step (stable near singular poses), capped at ~6 deg per iteration.
        step = J.T @ np.linalg.solve(J @ J.T + 1e-5 * np.eye(6), e)
        q = np.clip(q + np.degrees(np.clip(step, -0.1, 0.1)), lo, hi)
        e = error(q)

    pos_err_mm = float(np.linalg.norm(e[:3]) * 1000.0)
    ang_err_deg = float(np.degrees(np.linalg.norm(e[3:]) / ORIENT_WEIGHT_M_PER_RAD))
    return _dict(q), pos_err_mm, ang_err_deg


# =============================================================================================
# The core planner: one straight line in space
# =============================================================================================

def plan_line(q_start, end_mm, R_end=None, *, speed_mm_s: float, label: str,
              workspace_mm: tuple[list[float], list[float]] | None = None) -> Path:
    """Plan a straight-line gripper move. lift, lower and travel are all thin wrappers around this.

    Inputs
        q_start      : joint angles where the arm is now, degrees (dict {1..6} or list of 6)
        end_mm       : where the gripper tip should end, mm, base frame
        R_end        : gripper orientation at the end (3x3). None = keep the start orientation
                       exactly (used by lift/lower). For travel, orientation is blended smoothly
                       from start to R_end along the way.
        speed_mm_s   : peak gripper speed along the line. Automatically lowered if any joint would
                       exceed MAX_JOINT_SPEED_DEG_S.
        label        : short text for logs, e.g. "lift 100 mm"
        workspace_mm : optional ((xmin, ymin, zmin), (xmax, ymax, zmax)) box the gripper must stay in

    Output
        Path (see class docstring). Raises PlanError if any sample fails a safety check.
    """
    q0 = _as_dict(q_start)
    for j, (lo, hi) in JOINT_LIMITS_DEG.items():
        if not lo - 0.5 <= q0[j] <= hi + 0.5:
            raise PlanError(f"{label}: start J{j}={q0[j]:.1f} deg is outside its limits [{lo}, {hi}]")

    p0, R0 = fk_mm(q0)
    p1 = np.asarray(end_mm, dtype=float)
    R1 = R0 if R_end is None else np.asarray(R_end, dtype=float)
    dist = float(np.linalg.norm(p1 - p0))
    if dist < 0.5:  # nothing to do: return a 1-sample path so callers can still chain moves
        return Path([0.0], [q0], label, p0, p0)

    # ---- 1. geometric samples along the line, IK at each -------------------------------------
    n = max(2, int(math.ceil(dist / SAMPLE_SPACING_MM)) + 1)
    samples_q: list[np.ndarray] = [_vec(q0)]
    worst_pos, worst_ang = 0.0, 0.0
    q_prev = q0
    for k in range(1, n):
        s = k / (n - 1)
        p_k = p0 + s * (p1 - p0)                    # point on the straight line
        R_k = R0 if R_end is None else _slerp(R0, R1, s)  # orientation at that point
        q_k, pe, ae = ik(q_prev, p_k, R_k)

        # Safety checks for this sample. Any failure stops the whole plan.
        if pe > POS_TOL_MM or ae > ANG_TOL_DEG:
            raise PlanError(f"{label}: IK missed sample {k}/{n - 1} at {np.round(p_k, 1)} mm "
                            f"by {pe:.1f} mm / {ae:.1f} deg (out of reach, or a joint limit is in the way)")
        jump = np.abs(_vec(q_k) - samples_q[-1])
        if jump.max() > MAX_JUMP_PER_MM_DEG * SAMPLE_SPACING_MM:
            j = int(jump.argmax()) + 1
            raise PlanError(f"{label}: J{j} jumps {jump.max():.1f} deg between samples {k - 1} and {k} "
                            f"(the arm would flip configuration)")
        if workspace_mm is not None:
            lo_b, hi_b = np.asarray(workspace_mm[0]), np.asarray(workspace_mm[1])
            if np.any(p_k < lo_b) or np.any(p_k > hi_b):
                raise PlanError(f"{label}: sample {k} at {np.round(p_k, 1)} mm leaves the workspace box")

        worst_pos, worst_ang = max(worst_pos, pe), max(worst_ang, ae)
        samples_q.append(_vec(q_k))
        q_prev = q_k

    # ---- 2. timing: minimum-jerk along the line, slowed down if a joint would be too fast -----
    notes: list[str] = []
    duration = 1.875 * dist / speed_mm_s  # min-jerk peak speed = 1.875 x average
    S = np.array(samples_q)                # shape (n, 6), joint angles at evenly spaced distances
    for _ in range(20):
        times, joints = _sample_in_time(S, duration)
        rates = np.abs(np.diff(np.array(joints), axis=0)) * CONTROL_HZ  # deg/s between ticks
        peak = float(rates.max()) if len(rates) else 0.0
        if peak <= MAX_JOINT_SPEED_DEG_S:
            break
        duration *= peak / MAX_JOINT_SPEED_DEG_S * 1.05
    else:
        raise PlanError(f"{label}: could not keep joint speeds under {MAX_JOINT_SPEED_DEG_S} deg/s")
    eff_speed = 1.875 * dist / duration
    if eff_speed < speed_mm_s - 0.5:
        notes.append(f"slowed to {eff_speed:.0f} mm/s to respect joint speed limits")

    return Path(times, [_dict(j) for j in joints], label, p0, p1, worst_pos, worst_ang, notes)


TURN_SPACING_DEG = 0.5         # plan_turn: orientation step between IK solves
DEFAULT_TURN_SPEED = 10.0      # deg/s peak turn speed of the gripper in plan_turn


def plan_turn(q_start, R_end, *, speed_deg_s: float = DEFAULT_TURN_SPEED, label: str = "turn in place") -> Path:
    """Turn the gripper in place: the tip stays where it is while the orientation blends to R_end.
    plan_line can't do this (no distance to travel = nothing to plan). Same checks and timing as plan_line."""
    q0 = _as_dict(q_start)
    p0, R0 = fk_mm(q0)
    R1 = np.asarray(R_end, dtype=float)
    ang = math.degrees(math.acos(max(-1.0, min(1.0, (float(np.trace(R1 @ R0.T)) - 1.0) / 2.0))))
    if ang < 0.2:
        return Path([0.0], [q0], label, p0, p0)
    n = max(2, int(math.ceil(ang / TURN_SPACING_DEG)) + 1)
    samples_q: list[np.ndarray] = [_vec(q0)]
    worst_pos, worst_ang = 0.0, 0.0
    q_prev = q0
    for k in range(1, n):
        q_k, pe, ae = ik(q_prev, p0, _slerp(R0, R1, k / (n - 1)))
        if pe > POS_TOL_MM or ae > ANG_TOL_DEG:
            raise PlanError(f"{label}: IK missed sample {k}/{n - 1} by {pe:.1f} mm / {ae:.1f} deg "
                            f"(out of reach, or a joint limit is in the way)")
        jump = np.abs(_vec(q_k) - samples_q[-1])
        if jump.max() > MAX_JUMP_PER_MM_DEG:
            j = int(jump.argmax()) + 1
            raise PlanError(f"{label}: J{j} jumps {jump.max():.1f} deg between samples {k - 1} and {k} "
                            f"(the arm would flip configuration)")
        worst_pos, worst_ang = max(worst_pos, pe), max(worst_ang, ae)
        samples_q.append(_vec(q_k))
        q_prev = q_k
    duration = 1.875 * ang / speed_deg_s
    S = np.array(samples_q)
    for _ in range(20):
        times, joints = _sample_in_time(S, duration)
        rates = np.abs(np.diff(np.array(joints), axis=0)) * CONTROL_HZ
        if float(rates.max()) <= MAX_JOINT_SPEED_DEG_S:
            break
        duration *= float(rates.max()) / MAX_JOINT_SPEED_DEG_S * 1.05
    else:
        raise PlanError(f"{label}: could not keep joint speeds under {MAX_JOINT_SPEED_DEG_S} deg/s")
    return Path(times, [_dict(j) for j in joints], f"{label} ({ang:.1f} deg)", p0, p0, worst_pos, worst_ang)


def _sample_in_time(S: np.ndarray, duration: float):
    """Turn evenly spaced geometric samples S (n x 6) into control-rate targets with a
    minimum-jerk speed profile. Joints between two geometric samples are linearly interpolated
    (samples are only ~1 mm apart, so this stays on the line to well under 0.1 mm)."""
    n = len(S)
    ticks = max(2, int(math.ceil(duration * CONTROL_HZ)) + 1)
    times, joints = [], []
    for t_i in range(ticks):
        t = min(duration, t_i / CONTROL_HZ)
        x = _min_jerk(t / duration) * (n - 1)  # fractional sample index along the line
        i = min(int(x), n - 2)
        f = x - i
        joints.append((1 - f) * S[i] + f * S[i + 1])
        times.append(t)
    return times, joints


# =============================================================================================
# The three moves the task plan uses
# =============================================================================================

def plan_lift(q_now, distance_mm: float = 100.0, *, speed_mm_s: float = DEFAULT_LIFT_SPEED,
              workspace_mm=None) -> Path:
    """Move the gripper straight UP, keeping its orientation (so the tube stays exactly as upright
    as it was).

    Inputs
        q_now       : current joint angles, degrees (dict {1..6} or list of 6)
        distance_mm : how far to go up, mm (positive). 100 = out of the stand; 60 = off a well.
        speed_mm_s  : peak speed (default 20 mm/s)
        workspace_mm: optional safety box, see plan_line

    Output: Path. Raises PlanError if the arm can't do it.
    """
    if distance_mm <= 0:
        raise ValueError("distance_mm must be positive for a lift")
    p0, _ = fk_mm(q_now)
    return plan_line(q_now, p0 + np.array([0.0, 0.0, distance_mm]), None,
                     speed_mm_s=speed_mm_s, label=f"lift {distance_mm:.0f} mm", workspace_mm=workspace_mm)


def plan_lower(q_now, distance_mm: float = 60.0, *, speed_mm_s: float = DEFAULT_LOWER_SPEED,
               workspace_mm=None) -> Path:
    """Move the gripper straight DOWN, keeping its orientation.

    Inputs
        q_now       : current joint angles, degrees
        distance_mm : how far to go down, mm (positive). 60 = onto a well; 100 = back into the stand.
        speed_mm_s  : peak speed (default 15 mm/s, slower than lifting because we approach glass)
        workspace_mm: optional safety box. Its z minimum is what stops a typo from driving into the table.

    Output: Path. Raises PlanError if the arm can't do it.
    """
    if distance_mm <= 0:
        raise ValueError("distance_mm must be positive for a lower")
    p0, _ = fk_mm(q_now)
    return plan_line(q_now, p0 - np.array([0.0, 0.0, distance_mm]), None,
                     speed_mm_s=speed_mm_s, label=f"lower {distance_mm:.0f} mm", workspace_mm=workspace_mm)


def plan_travel(q_now, target, *, speed_mm_s: float = DEFAULT_TRAVEL_SPEED,
                height_offset_mm: float = 0.0, label: str | None = None, workspace_mm=None) -> Path:
    """Slide the gripper in a straight line in the SAME horizontal plane to sit above a target.

    The height never changes during travel: the end point is the target's x, y at the CURRENT z.
    Orientation turns smoothly from the start orientation to the target's, but only about the
    vertical axis, so a tube that is upright at both ends stays upright the whole way.

    Inputs
        q_now   : current joint angles, degrees
        target  : where to go. Either
                    - joint angles (dict {1..6} or list of 6), e.g. a taught spot from spots.yaml.
                      We use FK of those angles for x, y and orientation, or
                    - a dict {"xy_mm": [x, y]} (optionally with "R": 3x3) to give coordinates directly.
        speed_mm_s       : peak sideways speed (default 30 mm/s)
        height_offset_mm : only used to REPORT the height mismatch; travel never changes height.
                           Pass the distance you plan to lower next (e.g. 60) and the Path's notes
                           will say if lowering by that much will miss the taught height.
        label            : log text (default "travel to (x, y)")
        workspace_mm     : optional safety box

    Output: Path. Raises PlanError if the line is unreachable or would tilt the tube.
    """
    p0, R0 = fk_mm(q_now)
    if isinstance(target, dict) and "xy_mm" in target:
        xy = np.asarray(target["xy_mm"], dtype=float)
        R1 = np.asarray(target.get("R", R0), dtype=float)
        z_target = None
    else:
        p_t, R1 = fk_mm(target)
        xy, z_target = p_t[:2], p_t[2]

    # Refuse orientation changes that would tilt the tube: the turn from R0 to R1 must leave the
    # world vertical axis (almost) where it is, i.e. be a rotation about vertical only.
    up = np.array([0.0, 0.0, 1.0])
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, float((R1 @ R0.T @ up) @ up)))))
    if tilt > MAX_TILT_CHANGE_DEG:
        raise PlanError(f"travel: target orientation differs from the current one by a {tilt:.1f} deg tilt, "
                        f"not just a turn about vertical. Re-teach the spot with the tube upright.")

    end = np.array([xy[0], xy[1], p0[2]])
    path = plan_line(q_now, end, R1, speed_mm_s=speed_mm_s,
                     label=label or f"travel to ({xy[0]:.0f}, {xy[1]:.0f}) mm", workspace_mm=workspace_mm)

    # Height check for the move that follows (travel itself never changes height).
    if z_target is not None and height_offset_mm:
        miss = (p0[2] - height_offset_mm) - z_target
        if abs(miss) > 3.0:
            path.notes.append(f"lowering {height_offset_mm:.0f} mm after this lands {miss:+.0f} mm "
                              f"from the taught height")
    return path


def plan_shift(q_now, dxy_mm, *, speed_mm_s: float = DEFAULT_TRAVEL_SPEED, label: str | None = None,
               workspace_mm=None) -> Path:
    """Slide the gripper by a 2-D offset in its CURRENT horizontal plane: pure translation.

    Height and orientation are locked for the whole move (an upright tube stays exactly as upright).

    Inputs
        q_now  : current joint angles, degrees
        dxy_mm : (dx, dy) in mm, arm base frame: x = away from the arm base, y = to the arm's left
        speed_mm_s, workspace_mm : as in plan_line
    Output: Path. Raises PlanError if any point of the line is unreachable at this height/orientation.
    """
    dx, dy = (float(v) for v in dxy_mm)
    p0, _ = fk_mm(q_now)
    return plan_line(q_now, p0 + np.array([dx, dy, 0.0]), None, speed_mm_s=speed_mm_s,
                     label=label or f"shift ({dx:+.0f}, {dy:+.0f}) mm", workspace_mm=workspace_mm)


# Heights for the vial -> compartment round trip (mm). Lifting 150 then lowering 150 puts the pour
# pose at the grab pose's height; the return reverses both.
TRANSFER_LIFT_MM = 150.0
TRANSFER_LOWER_MM = 150.0


def plan_transfer(q_grab, dxy_mm, *, lift_mm: float = TRANSFER_LIFT_MM, lower_mm: float = TRANSFER_LOWER_MM,
                  workspace_mm=None) -> list[Path]:
    """Out trip, from the pose a grab ends in (tube held in its slot) to the pour-ready pose:
        lift lift_mm  ->  shift (dx, dy) in that locked horizontal plane  ->  lower lower_mm
    Inputs: q_grab = joint angles after the grab (degrees); dxy_mm = 2-D distance vial -> compartment (mm).
    Output: [lift, shift, lower] Paths, all planned before anything moves."""
    lift = plan_lift(q_grab, lift_mm, workspace_mm=workspace_mm)
    shift = plan_shift(lift.final_joints, dxy_mm, label=f"carry ({dxy_mm[0]:+.0f}, {dxy_mm[1]:+.0f}) mm",
                       workspace_mm=workspace_mm)
    lower = plan_lower(shift.final_joints, lower_mm, workspace_mm=workspace_mm)
    return [lift, shift, lower]


def plan_return(q_pour, dxy_mm, *, lift_mm: float = TRANSFER_LIFT_MM, lower_mm: float = TRANSFER_LOWER_MM,
                workspace_mm=None) -> list[Path]:
    """Return trip: the out trip with every parameter reversed.
        lift lower_mm  ->  shift (-dx, -dy)  ->  lower lift_mm
    Inputs: q_pour = joint angles at the pour-ready pose (tube upright again); dxy_mm = the SAME
    vial -> compartment offset that plan_transfer used. Ends where the grab ended (within IK tolerance)."""
    up = plan_lift(q_pour, lower_mm, workspace_mm=workspace_mm)
    back = plan_shift(up.final_joints, (-float(dxy_mm[0]), -float(dxy_mm[1])),
                      label=f"carry back ({-dxy_mm[0]:+.0f}, {-dxy_mm[1]:+.0f}) mm", workspace_mm=workspace_mm)
    down = plan_lower(back.final_joints, lift_mm, workspace_mm=workspace_mm)
    return [up, back, down]


def plan_sequence(q_now, steps: list[tuple], workspace_mm=None) -> list[Path]:
    """Plan several moves back to back; each starts where the previous one ends.

    Inputs
        q_now : current joint angles, degrees
        steps : list like [("lift", 100), ("travel", spot_q), ("lower", 60)]
                  ("lift", mm) | ("lower", mm) | ("travel", target) | ("shift", (dx, dy))
    Output
        list of Paths. Planning the whole sequence first means a failure in step 3 is found
        BEFORE step 1 starts moving.
    """
    paths, q = [], _as_dict(q_now)
    for kind, arg in steps:
        if kind == "lift":
            p = plan_lift(q, arg, workspace_mm=workspace_mm)
        elif kind == "lower":
            p = plan_lower(q, arg, workspace_mm=workspace_mm)
        elif kind == "travel":
            p = plan_travel(q, arg, workspace_mm=workspace_mm)
        elif kind == "shift":
            p = plan_shift(q, arg, workspace_mm=workspace_mm)
        else:
            raise ValueError(f"unknown step {kind!r}")
        paths.append(p)
        q = p.final_joints
    return paths
