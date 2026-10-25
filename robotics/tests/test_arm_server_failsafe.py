"""arm_server's failsafe on a simulated CAN bus: when the motors' replies stop (the receive side dies, commands still
get through - what dropped the arm on 2026-10-24), it must hold, ease to rest on commands alone, and only then
disable. Never disable while the arm is up."""
import math
import sys
import threading
import time

import pytest

import arm_server
from robstride_can import JOINTS

UP = {1: 10.0, 2: 90.0, 3: 60.0, 4: -30.0, 5: 5.0, 6: 0.0, 7: 0.0}   # arm held up, degrees from zero


class FakeBus:
    """Motors that follow MIT position commands instantly and report back, until `rx_dead` is set."""

    def __init__(self):
        self.state, self.rx_dead, self.log, self.cmd = {}, False, [], {}
        for mid in JOINTS:
            self._report(mid, math.radians(UP[mid]))

    def _report(self, mid, pos):
        if not self.rx_dead:
            self.state[mid] = dict(pos=pos, vel=0.0, torque=0.0, temp=30.0, fault=0, mode=2, t=time.time())

    def drain(self, timeout=0.0):
        pass

    def read_param_f32(self, mid, index, timeout=0.05):
        return self.state[mid]["pos"]

    def write_param_u8(self, *a, **k):
        pass

    def enable(self, mid):
        self.log.append(("enable", mid))

    def disable(self, mid, clear_fault=False):
        # what matters: where was the arm COMMANDED when its motor was let go?
        self.log.append(("disable", mid, math.degrees(self.cmd.get(mid, math.radians(UP[mid])))))

    def mit(self, mid, pos, kp, kd, vel=0.0, torque=0.0):
        if kp:  # arm joints (the gripper is torque-only)
            self.cmd[mid] = pos
        self._report(mid, self.cmd.get(mid, pos))

    def close(self):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(arm_server, "RobStrideBus", lambda: bus)
    monkeypatch.setattr(arm_server, "load_follower_zero", lambda: {m: 0.0 for m in JOINTS})
    monkeypatch.setattr(arm_server, "fine_timer", lambda: None)
    for name in ("CMD_FILE", "STATUS_FILE", "TILT_FILE", "STOP_FILE"):
        monkeypatch.setattr(arm_server, name, tmp_path / name.lower())
    monkeypatch.setattr(arm_server.arm_trace, "ENABLED", False)
    monkeypatch.setattr(sys, "argv", ["arm_server.py", "--no-comp"])
    return bus


def test_lost_feedback_eases_to_rest_before_disabling(server):
    threading.Timer(0.5, lambda: setattr(server, "rx_dead", True)).start()   # the replies stop mid-hold
    t0 = time.time()
    arm_server.main()
    first_enable = next(k for k, e in enumerate(server.log) if e[0] == "enable")   # startup disables don't count
    disables = [e for e in server.log[first_enable:] if e[0] == "disable" and e[1] != 7]
    assert disables, "motors were never disabled"
    # every arm joint was commanded back to rest (0 deg) before its motor was let go
    assert all(abs(e[2]) < 0.5 for e in disables[-6:]), disables[-6:]
    assert time.time() - t0 > arm_server.FEEDBACK_GRACE_S   # it held, waiting for the feedback first
