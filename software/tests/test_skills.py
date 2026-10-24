import pytest

from laminar import telemetry
from laminar.hardware import make_system


def run_job(s, vial="V1", liquid="blue", ml=2.0, tol=0.2):
    slot = s.skills.locate(vial).slot
    results = {}
    for name, step in [
        ("pick", lambda: s.skills.pick(vial)),
        ("carry", lambda: s.skills.carry("station")),
        ("pour", lambda: s.skills.pour("tube1", ml, liquid)),
        ("verify", lambda: s.skills.verify("tube1", ml, tol)),
        ("carry_back", lambda: s.skills.carry("rack")),
        ("place", lambda: s.skills.place(vial, slot)),
    ]:
        results[name] = r = step()
        if not r.ok:
            break
    return results


def test_clean_job_succeeds(system):
    res = run_job(system)
    assert all(r.ok for r in res.values()), {k: r.reason for k, r in res.items()}
    assert len(res) == 6
    assert system.skills.world.rack["A"] == "V1"
    assert system.skills.world.held is None


@pytest.mark.parametrize("seed", range(10))
def test_water_pour_within_0_2_ml(seed):
    res = run_job(make_system(fake=True, time_scale=0, seed=seed))
    assert abs(res["pour"].telemetry["dispensed_ml"] - 2.0) <= 0.2


def test_viscous_pour_within_0_4_ml(system):
    res = run_job(system, vial="V2", liquid="red", ml=1.5, tol=0.4)
    assert res["verify"].ok, res["verify"].reason


def test_wrong_tube_is_caught_at_verify():
    s = make_system(fake=True, time_scale=0, faults={"wrong_tube"})
    res = run_job(s)
    assert res["pour"].ok
    assert not res["verify"].ok
    assert "wrong tube" in res["verify"].reason


def test_grasp_slip_fails_pick():
    res = run_job(make_system(fake=True, time_scale=0, faults={"grasp_slip"}))
    assert not res["pick"].ok and "grasp" in res["pick"].reason


def test_drop_in_carry_fails_carry():
    res = run_job(make_system(fake=True, time_scale=0, faults={"drop_in_carry"}))
    assert not res["carry"].ok and "dropped" in res["carry"].reason


def test_level_stuck_aborts_pour():
    res = run_job(make_system(fake=True, time_scale=0, faults={"level_stuck"}))
    assert not res["pour"].ok and "not rising" in res["pour"].reason


def test_overshoot_is_caught_at_verify():
    res = run_job(make_system(fake=True, time_scale=0, faults={"pour_overshoot"}))
    assert not res["verify"].ok and res["verify"].telemetry["error_ml"] > 0.2


def test_pour_rejects_wrong_liquid(system):
    system.skills.pick("V1")
    system.skills.carry("station")
    r = system.skills.pour("tube1", 2.0, "red")
    assert not r.ok and "blue" in r.reason


def test_estop_mid_job_returns_failure_not_exception(system):
    system.skills.pick("V1")
    system.arm.stop()
    r = system.skills.carry("station")
    assert not r.ok and r.telemetry.get("estop")


def test_unknown_fault_rejected():
    with pytest.raises(ValueError):
        make_system(fake=True, time_scale=0, faults={"gremlins"})


def test_skills_log_start_end_and_pour_samples(system, tmp_log):
    run_job(system)
    events = [e["event"] for e in telemetry.read(tmp_log)]
    assert events.count("skill.start") == events.count("skill.end") == 6
    assert events.count("pour.sample") > 10
