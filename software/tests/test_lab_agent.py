import re

import pytest

from laminar.hardware import make_system
from laminar.lab_agent import Intent, LabAgent, bench_view, build_plan, parse_request, validate_plan

ME, STRANGER = "agent1qme", "agent1qstranger"


@pytest.fixture
def lab():
    return LabAgent(make_system(fake=True, time_scale=0), approvers={ME}, interpret=parse_request)


def _plan_id(reply: str) -> str:
    return re.search(r"Plan ([0-9a-f]{6})", reply).group(1)


def test_bench_answer_lists_vials(lab):
    r = lab.handle(ME, "what's on the bench?")
    assert "V1 in slot A: blue, 8 mL" in r and "V2 in slot B: red" in r and "tube1" in r


def test_parser_reads_volume_liquid_and_commands(lab):
    view = bench_view(lab.system)
    i = parse_request("Pour 1.5 ml of red into tube1 please", view)
    assert (i.action, i.ml, i.liquid, i.target) == ("plan", 1.5, "red", "tube1")
    assert parse_request("approve 3f2a1b", view) == Intent("approve", plan_id="3f2a1b")
    assert parse_request("cancel 3f2a1b", view).action == "cancel"
    assert parse_request("pour some blue", view).action == "unclear"


def test_full_workflow_in_chat(lab):
    r = lab.handle(ME, "pour 2 mL of blue into tube1")
    assert "pick(vial_id=V1)" in r and "pour(target=tube1, ml=2.0, liquid=blue)" in r
    pid = _plan_id(r)
    done = lab.handle(ME, f"approve {pid}")
    assert "DONE" in done and "verify: ok" in done
    assert lab.plans[pid].status == "done"
    assert "DONE" in lab.handle(ME, "runs")


def test_only_approvers_can_run_real_hardware_plans(lab):
    lab.simulated = False
    pid = _plan_id(lab.handle(STRANGER, "pour 1 mL of blue"))
    r = lab.handle(STRANGER, f"approve {pid}")
    assert "not on the approver list" in r and lab.plans[pid].status == "pending"
    assert "DONE" in lab.handle(ME, f"approve {pid}")


def test_cancel_and_double_approve(lab):
    pid = _plan_id(lab.handle(ME, "pour 1 mL of red"))
    assert "cancelled" in lab.handle(ME, f"cancel {pid}")
    assert "already cancelled" in lab.handle(ME, f"approve {pid}")


def test_validation_refuses_before_anything_moves(lab):
    r = lab.handle(ME, "pour 9 mL of blue")
    assert "fails the safety checks" in r and "between 0 and 5" in r
    r = lab.handle(ME, "pour 2 mL of green")
    assert "no green vial" in r
    assert lab.plans == {}


def test_validator_catches_broken_plans(lab):
    view = bench_view(lab.system)
    plan, _ = build_plan(Intent("plan", liquid="blue", ml=2.0), view, "x")
    assert validate_plan(plan, view) == []
    bad = {"steps": [plan["steps"][2]] + plan["steps"][4:5]}            # pour without picking, never placed
    problems = validate_plan(bad, view)
    assert any("nothing in the gripper" in p for p in problems)
    wrong = {"steps": [dict(s) for s in plan["steps"]]}
    wrong["steps"][2] = {"skill": "pour", "args": {"target": "tube1", "ml": 2.0, "liquid": "red"}}
    assert any("holds blue, not red" in p for p in validate_plan(wrong, view))


def test_failed_run_is_reported_with_the_abort(lab):
    from laminar.executor import Executor
    lab.system = make_system(fake=True, time_scale=0, faults=["wrong_tube"])
    lab.executor_factory = lambda: Executor(lab.system)
    pid = _plan_id(lab.handle(ME, "pour 2 mL of blue"))
    r = lab.handle(ME, f"approve {pid}")
    assert "ABORTED" in r and "Aborted at step" in r


def test_interpreter_failure_falls_back_to_rules(lab):
    def broken(text, view):
        raise ConnectionError("no network")
    lab.interpret = broken
    assert "Plan " in lab.handle(ME, "pour 1 mL of blue")


def test_fetch_agent_builds(monkeypatch):
    pytest.importorskip("uagents")
    from laminar import fetch_agent
    monkeypatch.setenv("LAMINAR_AGENT_SEED", "laminar test seed, not used for real")
    agent = fetch_agent.make_agent(LabAgent(make_system(fake=True, time_scale=0), approvers=set(),
                                            interpret=parse_request))
    assert agent.address.startswith("agent1q")
