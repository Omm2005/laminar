"""One RobStride joint over MotorBridge, position mode. For bench tests until Avery's RealArm exists.

UNTESTED ON HARDWARE. Call shapes follow the motorbridge README examples
(add_robstride_motor(id, 0xFD, model), ensure_mode(Mode.POS_VEL, 1000), send_pos_vel(rad, rad/s)).
Check them against motorbridge/bindings/python/examples/robstride_wrapper_demo.py first.
"""
from __future__ import annotations

import time

from .arm import Q_MAX, Q_MIN


class Joint:
    def __init__(self, channel: str = "can0@1000000", motor_id: int = 6, model: str = "rs-00",
                 feedback_id: int = 0xFD, vlim: float = 0.5):
        self.channel, self.motor_id, self.model, self.feedback_id, self.vlim = channel, motor_id, model, feedback_id, vlim
        self.lo, self.hi = (Q_MIN[motor_id - 1], Q_MAX[motor_id - 1]) if 1 <= motor_id <= 6 else (-0.5, 0.5)
        self.ctrl = self.m = None

    def connect(self) -> None:
        try:
            from motorbridge import Controller, Mode
        except ImportError:
            raise SystemExit("motorbridge is not installed: pip install motorbridge") from None
        self.ctrl = Controller(self.channel)
        self.m = self.ctrl.add_robstride_motor(self.motor_id, self.feedback_id, self.model)
        self.ctrl.enable_all()
        self.m.ensure_mode(Mode.POS_VEL, 1000)

    def state(self):
        poll = getattr(self.ctrl, "poll_feedback_once", None)
        if poll:
            poll()
        return self.m.get_state()

    def position(self) -> float | None:
        s = self.state()
        for key in ("pos", "position", "q", "angle"):
            v = s.get(key) if isinstance(s, dict) else getattr(s, key, None)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        if not self.lo <= pos <= self.hi:
            raise ValueError(f"joint{self.motor_id} target {pos:.3f} rad outside [{self.lo}, {self.hi}]")
        self.m.send_pos_vel(pos, vlim or self.vlim)

    def close(self) -> None:
        if self.m is not None:
            try:
                self.m.disable()
            finally:
                close = getattr(self.ctrl, "close", None)
                if close:
                    close()


class FakeJoint(Joint):
    """Simulated joint that moves toward its target at vlim in real time. Same interface as Joint."""

    def __init__(self, motor_id: int = 6, start: float = 0.0, **_):
        super().__init__(motor_id=motor_id)
        self._pos, self._target, self._v, self._t = start, start, self.vlim, time.monotonic()

    def connect(self) -> None:
        pass

    def _update(self) -> None:
        now = time.monotonic()
        step = self._v * (now - self._t)
        self._t = now
        d = self._target - self._pos
        self._pos += max(-step, min(step, d))

    def state(self):
        self._update()
        return {"pos": round(self._pos, 4), "vel": self._v, "fake": True}

    def position(self) -> float | None:
        self._update()
        return self._pos

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        if not self.lo <= pos <= self.hi:
            raise ValueError(f"joint{self.motor_id} target {pos:.3f} rad outside [{self.lo}, {self.hi}]")
        self._update()
        self._target, self._v = pos, vlim or self.vlim

    def close(self) -> None:
        pass
