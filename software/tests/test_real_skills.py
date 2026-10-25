"""Real-arm skills end to end without hardware: real motion planning (robotics/motion.py, episode.plan_a_to_b) and
the real agent + executor, with a stand-in arm_server client, side camera and recorded grab."""
from __future__ import annotations

import types

import pytest

from laminar.executor import Executor
from laminar.hardware import System
from laminar.lab_agent import LabAgent, bench_view, validate_plan
from laminar.real_skills import RealArm, RealSkills

GRASP = {1: -31.0, 2: 100.0, 3: 11.0, 4: 88.0, 5: 29.0, 6: -5.0}   # real demo grab pose (robotics tests)


def _spot_over_well():
    """A reachable pour spot: where the old transfer moves end from the grab pose."""
    import motion as M
    return dict(M.plan_transfer(GRASP, (40.0, -120.0), lift_mm=150, lower_mm=110)[-1].final_joints)


LAYOUT = {
    "lift_mm": 150, "lower_mm": 110, "vial_ml": 8,
    "pour": {"joint": 6, "direction": 1, "columns": {"well_1": "A", "well_2": "B"}, "capacity_ml": 4.5,
             "max_tilt_deg": 85},
    "pour_spots": {"well_2": "vial_A_final_position"},                     # well_1 has no pour spot
    "routes": {"vial_A": {"well_1": None, "well_2": None}, "vial_B": {"well_1": None, "well_2": None}},
    "vials": {"vial_A": "blue", "vial_B": "red"},
}
GRAB = {"name": "vial_A", "hz": 20.0, "initial_position": {**GRASP, 7: 20.0},
        "points": [{**{j: 0.0 for j in range(1, 7)}, 7: 78.0}, {**GRASP, 7: 20.0}]}


class FakeServer:
    """arm_server as ArmClient sees it: paths and recorded motions move the commanded pose."""

    dry_run = False

    def __init__(self):
        self.q = {j: 0.0 for j in range(1, 7)}            # at rest
        self.grip, self.log = 0.0, []

    def current_joints(self):
        return dict(self.q)

    def run(self, path, mode="path"):
        self.log.append(path.label)
        self.q = path.final_joints
        return {"result": "ok"}

    def status(self):
        return {"gripper": {"pos": self.grip}}

    def _send(self, cmd, wait=True):
        op = cmd["op"]
        self.log.append(op)
        if op == "gripper":
            self.grip = cmd["target"]
        elif op == "move":
            self.q = {int(j): v for j, v in cmd["joints"].items()}
        elif op == "path":
            last = cmd["points"][-1]
            self.q = {j: last[str(j)] for j in range(1, 7)}
            self.grip = last["7"]
        return {"result": "ok"}


class FakeCamera:
    """Side camera on the compartments: pour_ml adds what was asked (times `gain`)."""

    def __init__(self, gain: float = 1.0, fail: str | None = None):
        self.ml = {"A": 0.0, "B": 0.5}
        self.gain, self.fail, self.column, self.closed = gain, fail, "A", False

    def level(self, fresh_s=1.0, column=None):
        ml = self.ml[column or self.column]
        return ml / 4.5, ml, 4.5

    def pour_ml(self, direction, add_ml, max_tilt, view=True, on_tick=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.ml[self.column] += add_ml * self.gain
        return (types.SimpleNamespace(ok=True, result="done", reason="target reached", max_tilt_deg=62.0,
                                      duration_s=4.2), round(add_ml * self.gain, 3))

    def close(self):
        self.closed = True


def make(camera=None, motions=None, placeholders=()):
    server, cam = FakeServer(), camera or FakeCamera()
    spots = {"vial_A_final_position": _spot_over_well(), "vial_A_initial_position": GRASP}
    skills = RealSkills(client=server, layout=LAYOUT, spots=spots, camera_factory=lambda column: cam,
                        motions={"vial_A": GRAB} if motions is None else motions, placeholders=placeholders)
    return System(RealArm(server), None, skills, {}), server, cam


def run_request(text, **kw):
    system, server, cam = make(**kw)
    a = LabAgent(system=system, approvers={"om"})
    reply = a.handle("om", text)
    pid = reply.split()[1].rstrip(",")
    return a, a.handle("om", f"approve {pid}"), server, cam, reply


def test_full_job_on_the_team_motions():
    a, report, server, cam, plan_reply = run_request("pour 1 ml of blue into well 2")
    assert "carry(to=well_2)" in plan_reply and "simulated" not in plan_reply
    assert "DONE" in report, report
    assert "measured_ml=1.0" in report and "error_ml=0.0" in report
    log = server.log
    assert log[:3] == ["move", "gripper", "path"]                         # the recorded grab
    assert log[3].startswith("lift") and log[4] == "travel A -> B" and log[5].startswith("lower")
    assert [p for p in log if p.startswith("back: ")] == [f"back: {p}" for p in reversed(log[3:6])]
    assert log[-3:] == ["move", "gripper", "path"]                        # the grab played backwards
    assert cam.ml["B"] == pytest.approx(1.5)                              # 0.5 mL already there + 1 mL
    assert a.system.skills.world.vials["vial_A"]["ml"] == pytest.approx(7.0)


def test_column_letter_names_the_compartment():
    system, _, _ = make()
    reply = LabAgent(system=system, approvers={"om"}).handle("om", "add 2 mL of the blue one to column B")
    assert "pour(target=well_2, ml=2.0, liquid=blue)" in reply


def test_no_pour_spot_is_refused_before_anything_moves():
    system, server, _ = make()
    reply = LabAgent(system=system, approvers={"om"}).handle("om", "pour 1 ml of blue into well 1")
    assert "no taught way to carry" in reply and server.log == []


def test_no_recorded_grab_is_refused_unless_placeholder():
    system, server, _ = make()
    reply = LabAgent(system=system, approvers={"om"}).handle("om", "pour 1 ml of red into well 2")
    assert "vial_B" in reply and server.log == []


def _with_vial_in_hand(**kw):
    system, server, cam = make(**kw)
    server.q, server.grip = dict(GRASP), 30.0                            # an operator put the vial in the gripper
    a = LabAgent(system=system, approvers={"om"})
    pid = a.handle("om", "pour 1 ml of blue into well 2").split()[1].rstrip(",")
    return a.handle("om", f"approve {pid}"), server


def test_placeholder_pick_prints_and_moves_nothing():
    report, server = _with_vial_in_hand(motions={}, placeholders=("pick", "place"))
    assert "DONE" in report, report
    assert "move" not in server.log and "path" not in server.log         # no grab sent to the arm


def test_placeholder_pick_with_an_empty_gripper_stops_before_carrying():
    _, report, server, _, _ = run_request("pour 1 ml of blue into well 2", motions={}, placeholders=("pick", "place"))
    assert "ABORTED" in report and "not holding anything" in report
    assert server.log == []


def test_camera_problem_aborts_with_the_vial_held_upright():
    _, report, server, _, _ = run_request("pour 1 ml of blue into well 2",
                                          camera=FakeCamera(fail="only 6 level readings a second"))
    assert "ABORTED" in report and "only 6 level readings" in report
    assert not any(p.startswith("back: ") for p in server.log)            # holds over the well


def test_overpour_fails_verify():
    _, report, _, _, _ = run_request("pour 1 ml of blue into well 2", camera=FakeCamera(gain=1.4))
    assert "ABORTED" in report and "gained 1.40 mL" in report


def test_lift_and_lower_on_their_own():
    system, server, _ = make()
    server.q = dict(GRASP)
    plan = {"plan_id": "x", "steps": [{"skill": "lift", "args": {"mm": 50}}, {"skill": "lower", "args": {"mm": 50}}]}
    rep = Executor(system).run(plan, approved_by="om")
    assert rep.status == "done", rep.to_dict()
    assert server.log == ["lift 50 mm", "lower 50 mm"]


def test_lift_out_of_range_is_refused():
    system, server, _ = make()
    problems = validate_plan({"steps": [{"skill": "lift", "args": {"mm": 400}}]}, bench_view(system))
    assert problems and server.log == []


def test_bench_view_says_what_is_not_set_up():
    system, _, _ = make()
    view = bench_view(system)
    assert view["real"] and view["routes"]["vial_A"] == ["well_2"]
    assert view["vials"]["vial_A"]["taught"] and not view["vials"]["vial_B"]["taught"]
    assert any("well_1" in p for p in view["not_ready"])


def test_claude_reads_a_loose_request_into_a_checked_plan():
    """Claude only fills the Intent; the plan is still built and validated by code."""
    import json
    from laminar.lab_agent import claude_interpreter

    class FakeClaude:
        calls = []
        beta = property(lambda self: self)
        messages = property(lambda self: self)

        def create(self, **kw):
            self.calls.append(kw)
            intent = {"action": "plan", "vial_id": "vial_A", "liquid": "blue", "ml": 1.5, "target": "well_2",
                      "plan_id": None, "note": ""}
            return types.SimpleNamespace(stop_reason="end_turn",
                                         content=[types.SimpleNamespace(type="text", text=json.dumps(intent))])

    claude = FakeClaude()
    system, server, _ = make()
    a = LabAgent(system=system, approvers={"om"}, interpret=claude_interpreter(claude))
    reply = a.handle("om", "one and a half mils of the blue stuff in the second compartment please")
    assert "pour(target=well_2, ml=1.5, liquid=blue)" in reply and server.log == []
    assert claude.calls[0]["model"] == "claude-opus-5-5"
