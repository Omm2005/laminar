"""The pour tilt with every joint, the gripper tip locked in place.

Wrist roll (J6) alone stops at its 90 deg limit, and a conical tube with a little liquid needs to go past horizontal
(100-120 deg) before anything leaves the mouth. Here the whole gripper tips about a horizontal axis through the
gripper tip, towards the container: inverse kinematics solves J1-J6 for every degree of tilt, so the tip stays where
it is (within IK tolerance) while J4/J5/J6 share the rotation.

    plan_tilt_table(q, toward_xy)   joints for 0, 1, 2, ... deg of tilt, as far as the arm can go
    best_pour_start(q, toward_xy)   the gripper heading (turned in place, about vertical) from which the tilt goes
                                    furthest: with the taught heading J6 runs out at ~90 deg; turned ~30 deg, J4
                                    takes over and the tilt reaches well past 120 deg
    TipTiltJoint(client, table)     the same interface as pour_motor6's ServerTiltJoint (position, move_to, lo, hi,
                                    close), so the level-model pour (pour_motor6._pour) drives it unchanged: tilt
                                    angle in, all six joints streamed to arm_server's pose_stream op

Units: degrees for joints and tilt, mm for positions, base frame (x forward, y left, z up).
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

import motion as M

TILT_STEP_DEG = 1.0        # table resolution
MAX_TILT_DEG = 150.0       # never plan further than this
MAX_STEP_JUMP_DEG = 6.0    # a joint moving more than this per degree of tilt = a configuration flip: stop the table
JOINT_SPEED_DEG_S = 45.0   # the tilt speed is capped so no joint goes faster than this
SEND_HZ = 25.0             # pose updates to arm_server
WANT_TILT_DEG = 120.0      # best_pour_start: prefer the smallest heading turn that reaches this far
HEADING_TRIES = (0, 15, -15, 30, -30, 45, -45, 60, -60)


def rot_axis(u, deg: float) -> np.ndarray:
    """Rotation by deg about the unit axis u (Rodrigues)."""
    u = np.asarray(u, float) / np.linalg.norm(u)
    a = math.radians(deg)
    K = np.array([[0.0, -u[2], u[1]], [u[2], 0.0, -u[0]], [-u[1], u[0], 0.0]])
    return np.eye(3) + math.sin(a) * K + (1.0 - math.cos(a)) * K @ K


def tilt_axis(toward_xy) -> np.ndarray:
    """Horizontal axis whose positive rotation tips the top of the vial (tool z) towards toward_xy (a direction)."""
    d = np.array([toward_xy[0], toward_xy[1], 0.0], float)
    return np.cross([0.0, 0.0, 1.0], d / np.linalg.norm(d))


def plan_tilt_table(q_start, toward_xy, max_deg: float = MAX_TILT_DEG) -> list[dict[int, float]]:
    """[joints at 0 deg, 1 deg, 2 deg, ...] of tilt from q_start, tip locked, until IK fails, a joint limit or a
    configuration flip stops it. Entry 0 is q_start."""
    q0 = {j: float(q_start[j]) for j in range(1, 7)}
    p, R = M.fk_mm(q0)
    axis = tilt_axis(toward_xy)
    table, q = [q0], q0
    n = int(max_deg / TILT_STEP_DEG)
    for k in range(1, n + 1):
        qn, pe, ae = M.ik(q, p, rot_axis(axis, k * TILT_STEP_DEG) @ R)
        qn = dict(qn)
        if pe > M.POS_TOL_MM or ae > M.ANG_TOL_DEG or max(abs(qn[j] - q[j]) for j in range(1, 7)) > MAX_STEP_JUMP_DEG:
            break
        table.append(qn)
        q = qn
    return table


def best_pour_start(q, toward_xy, want: float = WANT_TILT_DEG, tries=HEADING_TRIES):
    """(heading turn deg, joints after the turn, tilt table from there): the smallest turn of the gripper about
    vertical (tip fixed) whose tilt reaches `want`, else the one that reaches furthest."""
    import markers as T
    p, R = M.fk_mm(q)
    best = None
    for g in tries:
        if g:
            qg, pe, ae = M.ik({**q, 1: q[1] + g}, p, T.yaw(g) @ R)
            if pe > M.POS_TOL_MM or ae > M.ANG_TOL_DEG:
                continue
            qg = dict(qg)
            # the turned pose being reachable is not enough: the turn itself must be (A2b 06:58 picked +60 deg,
            # whose end was fine but whose path ran into J4's limit halfway, and stopped with the vial over b)
            try:
                M.plan_turn(q, M.fk_mm(qg)[1], label="turn to the pour heading")
            except M.PlanError:
                continue
        else:
            qg = dict(q)
        table = plan_tilt_table(qg, toward_xy)
        reach = (len(table) - 1) * TILT_STEP_DEG
        if reach >= want:
            return g, qg, table
        if best is None or reach > (len(best[2]) - 1) * TILT_STEP_DEG:
            best = (g, qg, table)
    return best


def joints_at(table: list[dict[int, float]], tilt_deg: float) -> dict[int, float]:
    """Joints for a tilt between table entries (linear between the 1 deg samples)."""
    x = max(0.0, min(len(table) - 1, tilt_deg / TILT_STEP_DEG))
    i = min(int(x), len(table) - 2) if len(table) > 1 else 0
    f = x - i
    if len(table) == 1:
        return dict(table[0])
    return {j: (1 - f) * table[i][j] + f * table[i + 1][j] for j in range(1, 7)}


def tilt_speed_cap(table, tilt_deg: float) -> float:
    """deg/s of tilt that keeps every joint under JOINT_SPEED_DEG_S around this tilt."""
    i = min(int(tilt_deg / TILT_STEP_DEG), len(table) - 2)
    if i < 0:
        return JOINT_SPEED_DEG_S
    gain = max(abs(table[i + 1][j] - table[i][j]) for j in range(1, 7)) / TILT_STEP_DEG
    return JOINT_SPEED_DEG_S / max(gain, 1e-3)


class TipTiltJoint:
    """The pour's 'motor 6', but tipping with every joint: positions are radians of tilt from the start (0 =
    upright, as the pour began), hi = the furthest tilt in the table. The table goes to arm_server once (table_tilt
    op); a thread then streams only the tilt angle at SEND_HZ (also the watchdog heartbeat), and arm_server smooths
    it and interpolates the joints at 100 Hz. close() tilts back and ends the op."""

    def __init__(self, client, table: list[dict[int, float]]):
        self.client, self.table = client, table
        self.lo, self.hi = 0.0, math.radians((len(table) - 1) * TILT_STEP_DEG)
        self.base_deg = 0.0
        self._pos = self._target = 0.0
        self._v = math.radians(30.0)
        self._t = time.monotonic()
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._started = False

    def _start(self) -> None:
        self.client.start_table_tilt(self.table, TILT_STEP_DEG, max_rate_deg_s=60.0,
                                     joint_rate_deg_s=JOINT_SPEED_DEG_S, timeout_s=0.5)
        self._started = self._running = True
        self._t = time.monotonic()
        self._thread = threading.Thread(target=self._heartbeat, name="tip-tilt", daemon=True)
        self._thread.start()

    def _update(self) -> None:
        now = time.monotonic()
        cap = math.radians(tilt_speed_cap(self.table, math.degrees(self._pos)))
        step = min(self._v, cap) * min(now - self._t, 0.1)
        self._t = now
        self._pos += max(-step, min(step, self._target - self._pos))
        self.client.set_tilt(math.degrees(self._pos))

    def _heartbeat(self) -> None:
        while self._running:
            with self._lock:
                if self._running:
                    self._update()
            time.sleep(1.0 / SEND_HZ)

    def position(self) -> float:
        with self._lock:
            if not self._started:
                self._start()
            return self._pos

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        with self._lock:
            if not self._started:
                self._start()
            self._target = max(self.lo, min(self.hi, pos))
            if vlim:
                self._v = vlim

    def close(self) -> None:
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._started:
            self.client.end_tilt_stream()
