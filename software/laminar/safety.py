"""
E-stop watchdog. Sense owns the real UNO Q serial source in sense/.

The UNO Q sends 'HB' at 20 Hz and 'ESTOP' on a button press. The watchdog calls
arm.stop() on ESTOP or after timeout_s (250 ms) with no message.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

from . import telemetry
from .arm import Arm


class HeartbeatSource(ABC):
    @abstractmethod
    def read_message(self, timeout_s: float) -> str | None:
        """Next message: 'HB', 'ESTOP', or None if nothing arrived within timeout_s."""


class Watchdog:
    def __init__(self, arm: Arm, source: HeartbeatSource, timeout_s: float = 0.25):
        self.arm, self.source, self.timeout_s = arm, source, timeout_s
        self.tripped: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def trip(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = reason
        self.arm.stop()
        telemetry.log("safety.trip", {"reason": reason})

    def _loop(self) -> None:
        last = time.monotonic()
        while self._running and not self.tripped:
            msg = self.source.read_message(0.05)
            now = time.monotonic()
            if msg == "ESTOP":
                self.trip("estop button")
            elif msg == "HB":
                last = now
            elif now - last > self.timeout_s:
                self.trip(f"heartbeat lost for {now - last:.2f} s")


class FakeHeartbeat(HeartbeatSource):
    """Healthy 20 Hz heartbeat until you press_estop() or go_silent() (simulates a yanked cable)."""

    def __init__(self, hz: float = 20.0):
        self.period = 1.0 / hz
        self._estop = False
        self._silent = False

    def press_estop(self) -> None:
        self._estop = True

    def go_silent(self) -> None:
        self._silent = True

    def read_message(self, timeout_s):
        if self._estop:
            return "ESTOP"
        if self._silent:
            time.sleep(timeout_s)
            return None
        time.sleep(min(timeout_s, self.period))
        return "HB"
