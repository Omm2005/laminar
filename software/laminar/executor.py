r"""Executor: runs an approved plan step by step on any Skills implementation (fake or real).

States: IDLE -> RUNNING -> DONE
                        \-> ABORTING -> ABORTED
        IDLE -> REJECTED (not approved, or the validator found problems; nothing moves)

For every step: check preconditions, run the skill, check success. On any failure,
cancel or e-stop it aborts to a safe pose and the run report says exactly why.
Claude never runs inside this loop; it only reads the report afterwards.

plan.json:
    {"plan_id": "...", "request": "Aliquot 2 mL of blue into tube 1",
     "steps": [{"skill": "pick", "args": {"vial_id": "V1"}}, ...]}
"""
from __future__ import annotations

import inspect
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from . import telemetry
from .arm import ArmError
from .bench import pose
from .hardware import System
from .skills import SkillResult, Skills

RUNS_DIR = telemetry.REPO_ROOT / "logs" / "runs"
STEP_SKILLS = ("pick", "carry", "pour", "verify", "place", "lift", "lower")
SAFE_POSE = "home"  # where the arm holds a vial upright after an abort


class State(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ABORTING = "aborting"
    DONE = "done"
    ABORTED = "aborted"
    REJECTED = "rejected"


@dataclass
class StepRecord:
    index: int
    skill: str
    args: dict
    status: str = "pending"  # pending | ok | failed | skipped
    reason: str = ""
    telemetry: dict = field(default_factory=dict)
    duration_s: float = 0.0


@dataclass
class RunReport:
    run_id: str
    plan_id: str
    request: str
    approved_by: str | None
    status: str = State.IDLE.value
    steps: list[StepRecord] = field(default_factory=list)
    abort: dict | None = None        # {"step", "skill", "reason", "safe_pose_reached", "holding"}
    rejected: list[str] = field(default_factory=list)
    results: dict = field(default_factory=dict)  # dispensed_ml, measured_ml, error_ml
    started_at: float = 0.0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, runs_dir: Path = RUNS_DIR) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        p = runs_dir / f"{self.run_id}.json"
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return p

    def summary(self) -> str:
        lines = [f"run {self.run_id} [{self.status.upper()}] {self.request}"]
        for s in self.steps:
            mark = {"ok": "OK  ", "failed": "FAIL", "skipped": "skip", "pending": "    "}[s.status]
            args = ", ".join(f"{k}={v}" for k, v in s.args.items())
            lines.append(f"  {mark} {s.index + 1}. {s.skill}({args}) {s.reason}".rstrip())
        if self.rejected:
            lines += ["  rejected:"] + [f"    - {r}" for r in self.rejected]
        if self.abort:
            a = self.abort
            lines.append(f"  abort at step {a['step'] + 1} ({a['skill']}): {a['reason']}")
            lines.append(f"  safe pose reached: {a['safe_pose_reached']} ({a['safe_pose_note']}), holding: {a['holding']}")
        if self.results:
            lines.append("  results: " + ", ".join(f"{k}={v}" for k, v in self.results.items()))
        lines.append(f"  {self.duration_s:.2f} s")
        return "\n".join(lines)


Validator = Callable[[dict, dict], list[str]]  # (plan, bench) -> list of problems, empty = valid


class Executor:
    def __init__(self, system: System, validator: Validator | None = None, runs_dir: Path = RUNS_DIR):
        self.system = system
        self.validator = validator
        self.runs_dir = runs_dir
        self.state = State.IDLE
        self._cancel = threading.Event()
        self._cancel_reason = ""
        # what the executor believes, from skill results only (never peeks into the fakes)
        self.holding: str | None = None
        self.at: str = "home"

    def cancel(self, reason: str = "cancelled by operator") -> None:
        """Safe to call from another thread (dashboard, voice). Takes effect between steps."""
        self._cancel_reason = reason
        self._cancel.set()

    def _set_state(self, report: RunReport, state: State) -> None:
        self.state = state
        report.status = state.value
        telemetry.log("executor.state", {"state": state.value})

    # ------------------------------------------------------------ run

    def run(self, plan: dict, approved_by: str | None) -> RunReport:
        run_id = uuid.uuid4().hex[:8]
        telemetry.configure(run_id=run_id)
        report = RunReport(run_id=run_id, plan_id=plan.get("plan_id", ""), request=plan.get("request", ""),
                           approved_by=approved_by, started_at=time.time())
        report.steps = [StepRecord(i, s.get("skill", "?"), dict(s.get("args", {})))
                        for i, s in enumerate(plan.get("steps", []))]
        self._cancel.clear()
        t0 = time.monotonic()
        telemetry.log("executor.run", {"plan_id": report.plan_id, "request": report.request,
                                       "approved_by": approved_by, "n_steps": len(report.steps)})

        problems = self._reject_reasons(plan, approved_by)
        if problems:
            report.rejected = problems
            for s in report.steps:
                s.status = "skipped"
            self._set_state(report, State.REJECTED)
            return self._finish(report, t0)

        self._set_state(report, State.RUNNING)
        for step in report.steps:
            if self._cancel.is_set():
                self._abort(report, step, self._cancel_reason)
                break
            ok = self._run_step(step)
            if not ok:
                self._abort(report, step, step.reason)
                break
        else:
            self._set_state(report, State.DONE)
        return self._finish(report, t0)

    def _reject_reasons(self, plan: dict, approved_by: str | None) -> list[str]:
        problems = []
        if not approved_by:
            problems.append("plan not approved")
        if not plan.get("steps"):
            problems.append("plan has no steps")
        if self.validator is not None:
            problems += self.validator(plan, self.system.bench)
        return problems

    def _run_step(self, step: StepRecord) -> bool:
        t0 = time.monotonic()
        telemetry.log("executor.step", {"index": step.index, "skill": step.skill, "args": step.args})
        why = self._precondition(step)
        if why:
            step.status, step.reason = "failed", f"precondition: {why}"
            return False
        fn = getattr(self.system.skills, step.skill)
        try:
            res: SkillResult = fn(**step.args)
        except Exception as e:  # a bug in a skill must still end in a clean abort
            res = SkillResult(False, f"internal error in {step.skill}: {type(e).__name__}: {e}")
        step.telemetry, step.duration_s = res.telemetry, round(time.monotonic() - t0, 3)
        why = res.reason if not res.ok else self._postcondition(step, res)
        if why:
            step.status, step.reason = "failed", why
            return False
        step.status = "ok"
        self._update_belief(step)
        return True

    def _precondition(self, step: StepRecord) -> str:
        skill, a = step.skill, step.args
        if skill not in STEP_SKILLS:
            return f"unknown skill '{skill}'"
        params = [p for p in inspect.signature(getattr(Skills, skill)).parameters if p != "self"]
        missing = [p for p in params if p not in a]
        extra = [k for k in a if k not in params]
        if missing or extra:
            return f"bad args for {skill}: missing {missing}, unexpected {extra}"
        if skill == "pick" and self.holding:
            return f"already holding {self.holding}"
        if skill == "carry" and a["to"] not in ("rack", "home") and not self.holding:
            return f"nothing held to carry to {a['to']}"
        if skill == "pour" and (not self.holding or self.at not in ("station", a["target"])):
            return f"must hold a vial at {a['target']} (or the station) before pouring"
        if skill == "place" and self.holding != a["vial_id"]:
            return f"not holding {a['vial_id']} (holding {self.holding})"
        return ""

    def _postcondition(self, step: StepRecord, res: SkillResult) -> str:
        if step.skill == "pick" and not self.system.arm.gripper_stalled():
            return "pick reported ok but gripper is not holding anything"
        if step.skill == "pour" and "dispensed_ml" not in res.telemetry:
            return "pour reported ok but gave no dispensed_ml"
        return ""

    def _update_belief(self, step: StepRecord) -> None:
        if step.skill == "pick":
            self.holding, self.at = step.args["vial_id"], "rack"
        elif step.skill == "carry":
            self.at = step.args["to"]
        elif step.skill == "place":
            self.holding, self.at = None, "rack"
        elif step.skill in ("lift", "lower"):
            self.at = "moved"

    # ------------------------------------------------------------ abort

    def _abort(self, report: RunReport, step: StepRecord, reason: str) -> None:
        self._set_state(report, State.ABORTING)
        if step.status == "pending":
            step.status, step.reason = "skipped", reason
        for s in report.steps[step.index + 1:]:
            s.status = "skipped"
        estop = bool(step.telemetry.get("estop")) or getattr(self.system.arm, "stopped", False)
        safe = False
        safe_stop = getattr(self.system.skills, "safe_stop", None)   # real arm: hold where it is
        if estop:
            reason_safe = "arm is e-stopped; not moving. A human must check the bench and reset."
        elif safe_stop:
            safe, reason_safe = safe_stop()
        else:
            try:
                self.system.arm.move_q(pose(self.system.bench, SAFE_POSE), 2.0)
                safe, reason_safe = True, f"holding at '{SAFE_POSE}'"
            except ArmError as e:
                reason_safe = f"could not reach safe pose: {e}"
        holding = self.holding if self.system.arm.gripper_stalled() else None
        if self.holding and not holding:
            reason_safe += f"; vial {self.holding} is not in the gripper, check the bench"
        report.abort = {"step": step.index, "skill": step.skill, "reason": reason, "estop": estop,
                        "safe_pose_reached": safe, "safe_pose_note": reason_safe, "holding": holding}
        telemetry.log("executor.abort", report.abort)
        self._set_state(report, State.ABORTED)

    def _finish(self, report: RunReport, t0: float) -> RunReport:
        report.duration_s = round(time.monotonic() - t0, 3)
        for s in report.steps:
            if s.skill == "pour" and "dispensed_ml" in s.telemetry:
                report.results["dispensed_ml"] = s.telemetry["dispensed_ml"]
            if s.skill == "verify" and "measured_ml" in s.telemetry:
                report.results["measured_ml"] = s.telemetry["measured_ml"]
                report.results["error_ml"] = s.telemetry["error_ml"]
        path = report.save(self.runs_dir)
        telemetry.log("executor.report", {"status": report.status, "path": str(path), "duration_s": report.duration_s})
        telemetry.configure(run_id=None)
        return report
