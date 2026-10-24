"""Arm contract (Robotics owns the real one in robotics/) and FakeArm for running without hardware.

Units: metres and radians. Joint order is joint1..joint6 as in robotics/urdf/ReBot_Arm_RS.urdf.
The gripper is driven separately through gripper(width_mm), not as a 7th joint.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence

import numpy as np

from . import telemetry

N_JOINTS = 6
# From robotics/urdf/ReBot_Arm_RS.urdf
Q_MIN = np.array([-2.80, 0.00, 0.00, -1.57, -1.57, -3.14])
Q_MAX = np.array([+2.80, 3.14, 3.14, +1.57, +1.57, +3.14])
GRIPPER_MAX_MM = 50.0  # placeholder, confirm on hardware


class ArmError(Exception):
    """A move was rejected (joint limits, workspace, IK) or failed."""


class ArmStopped(ArmError):
    """The arm is e-stopped. Nothing moves until it is cleared."""


class Arm(ABC):
    @abstractmethod
    def get_q(self) -> list[float]:
        """6 joint angles, rad."""

    @abstractmethod
    def move_q(self, q: Sequence[float], duration_s: float) -> None:
        """Blocking S-curve move to a joint target."""

    @abstractmethod
    def move_pose(self, T: np.ndarray, duration_s: float, keep_upright: bool = True) -> None:
        """Blocking move of the gripper_end frame to 4x4 pose T in the arm base frame. IK inside."""

    @abstractmethod
    def stream_q(self, q_list: Sequence[Sequence[float]], dt: float) -> None:
        """Stream joint targets every dt seconds (50 Hz for carry/pour). Blocking."""

    @abstractmethod
    def gripper(self, width_mm: float) -> None:
        """Open or close the gripper to width_mm. Stops early on contact."""

    @abstractmethod
    def gripper_stalled(self) -> bool:
        """True = fingers stopped on something, i.e. holding an object."""

    @abstractmethod
    def stop(self) -> None:
        """E-stop path. Non-blocking and safe to call from any thread."""


def check_limits(q: Sequence[float]) -> np.ndarray:
    q = np.asarray(q, dtype=float)
    if q.shape != (N_JOINTS,):
        raise ArmError(f"expected {N_JOINTS} joint angles, got shape {q.shape}")
    bad = np.where((q < Q_MIN - 1e-6) | (q > Q_MAX + 1e-6))[0]
    if bad.size:
        i = bad[0]
        raise ArmError(f"joint{i + 1} = {q[i]:.3f} rad outside [{Q_MIN[i]:.2f}, {Q_MAX[i]:.2f}]")
    return q


class FakeArm(Arm):
    """Remembers its joint state, enforces limits and the workspace box, and logs every call.

    time_scale: 1.0 = moves take real time, 0 = instant (tests).
    object_mm: set by the fake world when a vial sits between the fingers.
    faults: {"grasp_slip"} makes the gripper close without stalling.
    """

    def __init__(self, q0: Sequence[float] | None = None, time_scale: float = 1.0,
                 workspace_m: tuple[np.ndarray, np.ndarray] | None = None):
        self.q = check_limits(q0 if q0 is not None else np.zeros(N_JOINTS))
        self.time_scale = time_scale
        self.workspace_m = workspace_m
        self.pose: np.ndarray | None = None
        self.width_mm = GRIPPER_MAX_MM
        self.object_mm: float | None = None
        self.faults: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        self._stalled = False
        self._stop = threading.Event()

    # fake-only helpers
    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def clear_stop(self) -> None:
        self._stop.clear()
        self._call("clear_stop")

    def _call(self, name: str, **args) -> None:
        self.calls.append((name, args))
        telemetry.log(f"arm.{name}", args)

    def _guard(self) -> None:
        if self._stop.is_set():
            raise ArmStopped("arm is e-stopped")

    def _sleep(self, s: float) -> None:
        if self.time_scale > 0:
            time.sleep(s * self.time_scale)
        self._guard()

    # contract
    def get_q(self) -> list[float]:
        return self.q.tolist()

    def move_q(self, q, duration_s):
        self._guard()
        target = check_limits(q)
        self._call("move_q", q=target, duration_s=duration_s)
        start, n = self.q.copy(), max(1, int(duration_s / 0.02))
        for k in range(1, n + 1):
            self.q = start + (target - start) * (k / n)
            self._sleep(duration_s / n)

    def move_pose(self, T, duration_s, keep_upright=True):
        self._guard()
        T = np.asarray(T, dtype=float)
        if T.shape != (4, 4):
            raise ArmError(f"expected a 4x4 transform, got shape {T.shape}")
        if self.workspace_m is not None:
            lo, hi = self.workspace_m
            xyz = T[:3, 3]
            if np.any(xyz < lo) or np.any(xyz > hi):
                raise ArmError(f"target {np.round(xyz, 3).tolist()} m outside workspace box")
        self._call("move_pose", xyz=T[:3, 3], duration_s=duration_s, keep_upright=keep_upright)
        self.pose = T
        self._sleep(duration_s)

    def stream_q(self, q_list, dt):
        self._guard()
        qs = [check_limits(q) for q in q_list]
        self._call("stream_q", n=len(qs), dt=dt)
        for q in qs:
            self.q = q
            self._sleep(dt)

    def gripper(self, width_mm):
        self._guard()
        if not 0 <= width_mm <= GRIPPER_MAX_MM:
            raise ArmError(f"gripper width {width_mm} mm outside [0, {GRIPPER_MAX_MM}]")
        holds = self.object_mm is not None and width_mm < self.object_mm and "grasp_slip" not in self.faults
        self.width_mm = self.object_mm if holds else width_mm
        self._stalled = holds
        self._call("gripper", width_mm=width_mm, stalled=holds)
        self._sleep(0.3)

    def gripper_stalled(self):
        return self._stalled

    def stop(self):
        self._stop.set()
        telemetry.log("arm.stop", {})
