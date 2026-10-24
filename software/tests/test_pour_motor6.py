import json

import pytest

from laminar.pour_motor6 import (PourConfig, SimClock, SimJoint, SimTracker, allowed_rate, check_calibration,
                                 pour_left, pour_right, simulate)


def test_rate_schedule_slows_at_30_60_80_90():
    assert allowed_rate(0.0) == allowed_rate(0.29) == 0.25
    assert allowed_rate(0.3) == 0.15
    assert allowed_rate(0.6) == 0.08
    assert allowed_rate(0.8) == 0.04
    assert allowed_rate(0.9) == allowed_rate(0.99) == 0.02


def _calib(tmp_path, columns=("A", "B"), curve=True, ref=True):
    data = {"columns": {c: {"roi": [0, 0, 10, 200], "curve": [[0, 0], [200, 4]] if curve else [[0, 0]]}
                        for c in columns}, "empty": ["X1", "X2"]}
    if ref:
        (tmp_path / "level_ref.png").write_bytes(b"x")
        data["ref_image"] = "level_ref.png"
    p = tmp_path / "level_calib.json"
    p.write_text(json.dumps(data))
    return p


def test_camera_calibration_ok(tmp_path):
    assert check_calibration(_calib(tmp_path), "A") == []


def test_camera_calibration_problems(tmp_path):
    assert "not found" in check_calibration(tmp_path / "nope.json", "A")[0]
    assert any("column C is not calibrated" in p for p in check_calibration(_calib(tmp_path), "C"))
    assert any("no syringe curve" in p for p in check_calibration(_calib(tmp_path, curve=False), "A"))
    assert any("reference" in p for p in check_calibration(_calib(tmp_path, ref=False), "A"))


def _rig(direction, scenario="normal"):
    clock = SimClock()
    joint = SimJoint(clock)
    return clock, joint, SimTracker(clock, joint, direction, scenario=scenario)


@pytest.mark.parametrize("fn", [pour_right, pour_left])
def test_pour_returns_without_moving_when_not_calibrated(tmp_path, fn):
    clock, joint, tracker = _rig(1)
    r = fn(joint, tracker, "A", calib_path=tmp_path / "missing.json", clock=clock, verbose=False)
    assert not r.ok and r.result == "not_started" and "not calibrated" in r.reason
    assert r.log == [] and joint.pos == 0.0


def test_pour_returns_when_the_camera_does_not_trust_the_column(tmp_path):
    clock, joint, tracker = _rig(1)
    tracker.read = lambda name: tracker._Sample(0.0, 0.0, 0.1, clock.t)   # blocked camera
    r = pour_right(joint, tracker, "A", calib_path=_calib(tmp_path), clock=clock, verbose=False)
    assert r.result == "not_started" and "camera not ready" in r.reason and joint.pos == 0.0


def test_calibrated_pour_runs(tmp_path):
    import pour_guide
    clock, joint, tracker = _rig(1)
    real, pour_guide.time = pour_guide.time, clock
    try:
        r = pour_right(joint, tracker, "A", calib_path=_calib(tmp_path), clock=clock, verbose=False)
    finally:
        pour_guide.time = real
    assert r.ok and 0.95 <= r.final_fill <= 1.05


@pytest.mark.parametrize("direction", [1, -1])
def test_pour_right_and_left_fill_column_a(direction):
    r = simulate(direction, "normal", verbose=False)
    assert r.ok and r.column == "A" and r.direction == ("right" if direction > 0 else "left")
    assert 0.95 <= r.final_fill <= 1.05


def test_pour_follows_the_rate_schedule():
    r = simulate(1, "normal", verbose=False)
    late = [x for x in r.log if x["fill"] >= 0.9]
    assert late and max(x["rate"] for x in late) < 0.02 * 2.5   # 2 %/s zone: never far above


def test_wrong_channel_stops():
    r = simulate(1, "wrong_column", verbose=False)
    assert not r.ok and "column C" in r.reason


def test_overfill_stops():
    r = simulate(1, "overfill", verbose=False)
    assert not r.ok and "overflow" in r.reason


def test_dry_vial_stops_at_max_tilt():
    r = simulate(-1, "dry", verbose=False)
    assert not r.ok and "no flow at max tilt" in r.reason


def test_camera_lost_mid_pour_stops():
    r = simulate(1, "blocked", verbose=False)
    assert not r.ok and "camera" in r.reason


def test_surge_still_finishes_on_target():
    r = simulate(1, "surge", verbose=False)
    assert r.ok and 0.95 <= r.final_fill <= 1.05


def test_ends_upright():
    r = simulate(-1, "normal", verbose=False)
    assert r.log[-1]["tilt_deg"] < 45        # was tilting
    clock, joint, tracker = _rig(-1)          # fresh rig: the pour always returns to its start angle
    assert joint.pos == 0.0


# ---- percent-full calibration

def test_target_is_a_fraction_of_full():
    from laminar.pour_motor6 import target_box_frac
    # box is 200 px tall, but "full" (100 %) is at 160 px
    calib = {"columns": {"A": {"roi": [0, 0, 10, 200]}},
             "fill_curve": {"A": [[0, 0], [40, 0.25], [80, 0.5], [120, 0.75], [160, 1.0]]}}
    assert target_box_frac(calib, "A", 1.0) == pytest.approx(0.8)      # 160 / 200
    assert target_box_frac(calib, "A", 0.5) == pytest.approx(0.4)
    assert target_box_frac(calib, "A", 0.9) == pytest.approx(0.72)
    assert target_box_frac({"columns": {"A": {"roi": [0, 0, 10, 200]}}}, "A", 0.9) is None   # mL calibration


def test_pour_uses_the_percent_calibration(tmp_path):
    import pour_guide
    from laminar.pour_motor6 import fake_calibrate
    calib = fake_calibrate(tmp_path / "level_calib_sim.json")
    clock, joint, tracker = _rig(1)
    real, pour_guide.time = pour_guide.time, clock
    try:
        r = pour_right(joint, tracker, "A", calib_path=calib, cfg=PourConfig(target_frac=0.5),
                       clock=clock, verbose=False)
    finally:
        pour_guide.time = real
    assert r.ok and 0.47 <= tracker.ml["A"] / 4.0 <= 0.55      # half of a full (4 mL) column
