import time

import numpy as np

from laminar import telemetry
from laminar.arm import FakeArm
from laminar.safety import FakeHeartbeat, Watchdog


def _watch(hb):
    arm = FakeArm(time_scale=0)
    wd = Watchdog(arm, hb)
    wd.start()
    return arm, wd


def test_healthy_heartbeat_does_not_trip():
    arm, wd = _watch(FakeHeartbeat())
    time.sleep(0.5)
    wd.close()
    assert not arm.stopped and wd.tripped is None


def test_silence_trips_within_400_ms():
    hb = FakeHeartbeat()
    arm, wd = _watch(hb)
    time.sleep(0.1)
    hb.go_silent()
    time.sleep(0.4)
    wd.close()
    assert arm.stopped and "heartbeat" in wd.tripped


def test_estop_button_trips():
    hb = FakeHeartbeat()
    arm, wd = _watch(hb)
    hb.press_estop()
    time.sleep(0.15)
    wd.close()
    assert arm.stopped and wd.tripped == "estop button"


def test_telemetry_writes_jsonl_with_numpy(tmp_log):
    telemetry.log("hello", {"q": np.array([0.1, 0.2]), "x": np.float64(1.5)})
    rec = telemetry.read(tmp_log)[-1]
    assert rec["event"] == "hello" and rec["q"] == [0.1, 0.2] and rec["x"] == 1.5 and rec["run_id"] == "test"
