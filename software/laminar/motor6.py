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


class RobStrideJoint(Joint):
    """One joint through the team's candleLight driver (robotics/robstride_can.py), MIT position control.

    Positions are rad relative to the calibrated zero in robotics/follower_calibration.json.
    Only this motor is enabled. A 100 Hz thread moves the setpoint toward the target at the
    requested speed and streams it. Lost feedback (250 ms), a motor fault or > 80 C disables it.
    """

    FEEDBACK_TIMEOUT = 0.25
    TEMP_LIMIT_C = 80.0

    def __init__(self, motor_id: int = 6, hz: float = 100.0, **_):
        super().__init__(motor_id=motor_id)
        self.hz = hz
        self.error: str | None = None
        self.bus = None
        self._thread = None
        self._running = False

    def connect(self) -> None:
        import math
        import sys
        import threading

        from .telemetry import REPO_ROOT
        sys.path.insert(0, str(REPO_ROOT / "robotics"))
        import macos_usb
        macos_usb.patch()
        from leader_arm import load_follower_zero
        from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus

        mid = self.motor_id
        _, self.kp, self.kd, lo_deg, hi_deg = JOINTS[mid]
        self.lo, self.hi = math.radians(lo_deg), math.radians(hi_deg)
        if mid == 6:  # wrist roll: same +-90 deg soft limit as robotics/arm_server.py (camera cable)
            self.lo, self.hi = max(self.lo, -math.pi / 2), min(self.hi, math.pi / 2)
        zero = load_follower_zero()[mid]
        self.bus = RobStrideBus()
        self.bus.drain(0.1)
        raw = self.bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
        if raw is None:
            self.bus.close()
            self.bus = None
            raise RuntimeError(f"J{mid} did not respond - is the arm powered and on the CAN bus?")
        deg = math.degrees(raw)
        zero += 360.0 * round((deg - zero) / 360.0)  # single-turn wrap after power-up
        self.zero_rad = math.radians(zero)
        self._cmd = self._target = raw - self.zero_rad
        self._v = self.vlim

        self.bus.disable(mid)
        self.bus.write_param_u8(mid, P_RUN_MODE, 0)  # MIT mode
        time.sleep(0.05)
        self.bus.enable(mid)
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=f"J{mid}", daemon=True)
        self._thread.start()
        time.sleep(0.3)  # hold where it is before anything moves
        if self.error:
            raise RuntimeError(self.error)

    def _loop(self) -> None:
        dt = 1.0 / self.hz
        mid = self.motor_id
        started = last = time.time()
        while self._running:
            now = time.time()
            elapsed, last = now - last, now  # real loop time: USB sends make it longer than dt
            d = self._target - self._cmd
            step = self._v * min(elapsed, 0.05)  # cap so a stalled loop never jumps the target
            self._cmd += max(-step, min(step, d))
            self.bus.mit(mid, self.zero_rad + self._cmd, self.kp, self.kd)
            st = self.bus.state.get(mid)
            now = time.time()
            why = None
            if st is None:
                if now - started > self.FEEDBACK_TIMEOUT:
                    why = f"no feedback from J{mid}"
            elif now - st["t"] > self.FEEDBACK_TIMEOUT:
                why = f"lost feedback from J{mid}"
            elif st["fault"]:
                why = f"J{mid} fault 0x{st['fault']:02x}"
            elif st["temp"] > self.TEMP_LIMIT_C:
                why = f"J{mid} temperature {st['temp']:.0f} C"
            if why:
                self.error = why
                self._running = False
                self.bus.disable(mid)
                return
            time.sleep(dt)

    def _check(self) -> None:
        if self.error:
            raise RuntimeError(f"motor stopped: {self.error}")

    def state(self):
        st = self.bus.state.get(self.motor_id) if self.bus else None
        if not st:
            return None
        return {"pos_rad": round(st["pos"] - self.zero_rad, 4), "vel": round(st["vel"], 3),
                "torque": round(st["torque"], 3), "temp_c": st["temp"], "fault": st["fault"]}

    def position(self) -> float | None:
        st = self.bus.state.get(self.motor_id) if self.bus else None
        return None if not st else st["pos"] - self.zero_rad

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        self._check()
        if not self.lo <= pos <= self.hi:
            raise ValueError(f"joint{self.motor_id} target {pos:.3f} rad outside [{self.lo:.2f}, {self.hi:.2f}]")
        self._v = vlim or self.vlim
        self._target = pos

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.bus:
            try:
                self.bus.disable(self.motor_id)
            finally:
                self.bus.close()
