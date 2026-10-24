import copy
import json
import threading
from pathlib import Path

import pytest

from laminar import telemetry
from laminar.executor import Executor, State
from laminar.hardware import make_system

PLANS = Path(__file__).resolve().parents[1] / "plans"
BLUE = json.loads((PLANS / "aliquot_2ml_blue.json").read_text())
RED = json.loads((PLANS / "aliquot_1_5ml_red.json").read_text())


@pytest.fixture
def ex(tmp_path):
    return lambda faults=(): Executor(make_system(fake=True, time_scale=0, faults=faults), runs_dir=tmp_path)


def test_clean_run_is_done_with_report(ex, tmp_path):
    r = ex().run(BLUE, approved_by="Om")
    assert r.status == State.DONE
    assert [s.status for s in r.steps] == ["ok"] * 6
    assert abs(r.results["error_ml"]) <= 0.2
    saved = json.loads((tmp_path / f"{r.run_id}.json").read_text())
    assert saved["status"] == "done" and saved["approved_by"] == "Om"


def test_viscous_plan_runs(ex):
    assert ex().run(RED, approved_by="Om").status == State.DONE


def test_unapproved_plan_is_rejected_and_nothing_moves(ex):
    e = ex()
    r = e.run(BLUE, approved_by=None)
    assert r.status == State.REJECTED and "plan not approved" in r.rejected
    assert all(s.status == "skipped" for s in r.steps)
    assert e.system.arm.calls == []


def test_validator_problems_reject_the_plan(ex):
    e = ex()
    e.validator = lambda plan, bench: ["volume exceeds tube capacity"]
    r = e.run(BLUE, approved_by="Om")
    assert r.status == State.REJECTED and r.rejected == ["volume exceeds tube capacity"]
    assert e.system.arm.calls == []


def test_wrong_tube_aborts_at_verify_to_safe_pose(ex):
    r = ex({"wrong_tube"}).run(BLUE, approved_by="Om")
    assert r.status == State.ABORTED
    assert [s.status for s in r.steps] == ["ok", "ok", "ok", "failed", "skipped", "skipped"]
    assert r.abort["skill"] == "verify" and "wrong tube" in r.abort["reason"]
    assert r.abort["safe_pose_reached"] and r.abort["holding"] == "V1"


def test_dropped_vial_is_reported(ex):
    r = ex({"drop_in_carry"}).run(BLUE, approved_by="Om")
    assert r.status == State.ABORTED and r.abort["skill"] == "carry"
    assert r.abort["holding"] is None and "not in the gripper" in r.abort["safe_pose_note"]


def test_estop_does_not_move_to_safe_pose(ex):
    e = ex()
    plan = copy.deepcopy(BLUE)
    orig_pick = e.system.skills.pick

    def pick_then_estop(**kw):
        res = orig_pick(**kw)
        e.system.arm.stop()  # button pressed right after the pick
        return res
    e.system.skills.pick = pick_then_estop
    r = e.run(plan, approved_by="Om")
    assert r.status == State.ABORTED and r.abort["estop"] and not r.abort["safe_pose_reached"]
    assert r.abort["skill"] == "carry"


def test_precondition_blocks_pour_before_carry(ex):
    plan = copy.deepcopy(BLUE)
    del plan["steps"][1]  # no carry to the station
    r = ex().run(plan, approved_by="Om")
    assert r.status == State.ABORTED and r.abort["skill"] == "pour"
    assert r.steps[1].reason.startswith("precondition")


def test_bad_args_and_unknown_skill_fail_before_running(ex):
    plan = copy.deepcopy(BLUE)
    plan["steps"][0] = {"skill": "pick", "args": {"vial": "V1"}}
    r = ex().run(plan, approved_by="Om")
    assert "bad args" in r.steps[0].reason
    plan["steps"][0] = {"skill": "pipette", "args": {}}
    r = ex().run(plan, approved_by="Om")
    assert "unknown skill" in r.steps[0].reason


def test_skill_crash_still_aborts_cleanly(ex):
    e = ex()
    e.system.skills.carry = lambda **kw: 1 / 0
    r = e.run(BLUE, approved_by="Om")
    assert r.status == State.ABORTED and "ZeroDivisionError" in r.abort["reason"]


def test_cancel_between_steps(ex):
    e = ex()
    orig = e.system.skills.carry

    def carry_and_cancel(**kw):
        threading.Thread(target=e.cancel, args=("operator said stop",)).start()
        return orig(**kw)
    e.system.skills.carry = carry_and_cancel
    r = e.run(BLUE, approved_by="Om")
    assert r.status == State.ABORTED and r.abort["reason"] == "operator said stop"


def test_run_logs_states_and_report(ex, tmp_log):
    r = ex().run(BLUE, approved_by="Om")
    events = telemetry.read(tmp_log)
    states = [e["state"] for e in events if e["event"] == "executor.state"]
    assert states == ["running", "done"]
    assert all(e["run_id"] == r.run_id for e in events if e["event"].startswith(("executor.", "skill.")))
