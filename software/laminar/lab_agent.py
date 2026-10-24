"""Lab agent logic behind the Fetch.ai chat agent (fetch_agent.py): one chat message in, one reply out.

No network and no uagents here, so it is fully testable. The workflow happens entirely in chat:

    "what's on the bench?"            -> vials, liquids and volumes, station tube        (look_at_bench)
    "pour 2 mL of blue into tube1"    -> request -> plan -> validate -> pending plan id   (plan + checks)
    "approve 3f2a"                    -> only an approver: runs it on the executor        (execute)
                                         -> step results, measured mL, error, aborts      (verify + report)
    "cancel 3f2a" / "runs" / "help"

Safety, in code (never left to the language model):
- the request is interpreted into a small Intent (vial, liquid, mL, target); the plan's steps are built
  by code from the Intent, never written by a model
- validate_plan() checks every step against the bench and the vial volumes before anything moves
- on real hardware only senders listed in LAMINAR_APPROVERS can approve; anyone may ask for a plan
- the executor still aborts to a safe pose on any failed precondition or check

Interpretation: a deterministic parser by default (no API calls). LAMINAR_PLANNER=claude interprets
free-form requests with Claude instead (needs ANTHROPIC_API_KEY); the same validation applies.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from . import telemetry
from .bench import rack_slots
from .executor import RunReport
from .hardware import System, make_system

MAX_ML = 5.0                 # one pour never exceeds this
DEFAULT_TOL_ML = 0.2
APPROVERS_ENV = "LAMINAR_APPROVERS"   # comma-separated sender addresses allowed to approve runs
PLANNER_ENV = "LAMINAR_PLANNER"       # "rules" (default, no API calls) or "claude"
CLAUDE_MODEL = "claude-opus-5-5"


@dataclass
class Intent:
    action: str                       # plan | bench | approve | cancel | runs | help | unclear
    vial_id: str | None = None
    liquid: str | None = None
    ml: float | None = None
    target: str | None = None
    plan_id: str | None = None
    note: str = ""                    # why the request is unclear, in words for the user


@dataclass
class Pending:
    plan: dict
    requester: str
    status: str = "pending"           # pending | running | done | aborted | rejected | cancelled
    report: dict | None = None
    log: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- bench view

def bench_view(system: System) -> dict:
    """What the agent can say about the bench: vials by slot, liquids, volumes, the station tube."""
    world = getattr(system.skills, "world", None)
    station = system.bench["station"]
    view = {"station_target": station["target_id"], "station_tube": station["tube_type"],
            "tube_capacity_ml": system.bench["tubes"][station["tube_type"]]["capacity_ml"],
            "slots": rack_slots(system.bench), "vials": {}}
    if world is not None:
        for slot, vid in world.rack.items():
            if vid:
                v = world.vials[vid]
                view["vials"][vid] = {"slot": slot, "liquid": v["liquid"], "ml": round(v["ml"], 2)}
        view["station_tube"] = world.station_tube
    return view


def describe_bench(view: dict) -> str:
    if not view["vials"]:
        return "The rack is empty."
    lines = [f"- {vid} in slot {v['slot']}: {v['liquid']}, {v['ml']:g} mL" for vid, v in view["vials"].items()]
    return ("On the bench:\n" + "\n".join(lines)
            + f"\nPour station: {view['station_target']} ({view['station_tube']}, holds {view['tube_capacity_ml']:g} mL).")


# ---------------------------------------------------------------- interpreting a request

_ML = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ml|mls|milliliters?|millilitres?|cc)\b", re.I)
COLORS = {"red", "blue", "green", "yellow", "orange", "purple", "pink", "clear", "water"}
_ID = re.compile(r"\b([0-9a-f]{6})\b", re.I)


def parse_request(text: str, view: dict) -> Intent:
    """Deterministic interpretation: no model, no API call."""
    t = text.strip().lower()
    word = t.split()[0] if t.split() else ""
    if word in ("approve", "approved", "yes", "go", "run") and _ID.search(t):
        return Intent("approve", plan_id=_ID.search(t).group(1))
    if word in ("cancel", "reject", "no") and _ID.search(t):
        return Intent("cancel", plan_id=_ID.search(t).group(1))
    if word in ("runs", "history", "status"):
        return Intent("runs")
    if word in ("help", "hi", "hello", "hey", "?") or not t:
        return Intent("help")
    if any(k in t for k in ("bench", "rack", "vials", "what's on", "what is on", "inventory")):
        return Intent("bench")

    m = _ML.search(t)
    liquids = sorted({v["liquid"] for v in view["vials"].values()} | COLORS)
    liquid = next((c for c in liquids if re.search(rf"\b{c}\b", t)), None)
    vial = next((vid for vid in view["vials"] if re.search(rf"\b{vid.lower()}\b", t)), None)
    target = view["station_target"] if view["station_target"].lower() in t.replace(" ", "") or "tube" in t else None
    if not m and not liquid and not vial:
        return Intent("unclear", note="I can pour a measured volume from a vial into the station tube, "
                                      "e.g. 'pour 2 mL of blue into tube1'.")
    if not m:
        return Intent("unclear", note="How many mL? e.g. 'pour 2 mL of blue'.")
    return Intent("plan", vial_id=vial, liquid=liquid, ml=float(m.group(1)), target=target)


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["plan", "bench", "approve", "cancel", "runs", "help", "unclear"]},
        "vial_id": {"type": ["string", "null"]},
        "liquid": {"type": ["string", "null"]},
        "ml": {"type": ["number", "null"]},
        "target": {"type": ["string", "null"]},
        "plan_id": {"type": ["string", "null"]},
        "note": {"type": "string"},
    },
    "required": ["action", "vial_id", "liquid", "ml", "target", "plan_id", "note"],
    "additionalProperties": False,
}


def claude_interpreter(client=None) -> Callable[[str, dict], Intent]:
    """Interpret free-form lab requests with Claude (structured output into INTENT_SCHEMA). The bench view is
    given as context so it can resolve 'the blue one' or 'half the red vial'. It only fills an Intent; the plan
    is still built and validated by code. Off unless LAMINAR_PLANNER=claude."""
    import anthropic
    client = client or anthropic.Anthropic()
    system = ("You turn a lab technician's chat message into an Intent for a robot arm that pours liquid from a "
              "vial into a tube. Use only vial ids, liquids and targets that exist in the bench JSON. "
              "action 'plan' needs ml; if the vial or volume is ambiguous, use action 'unclear' and put a short "
              "question in note. 'approve <id>' / 'cancel <id>' carry the 6-character plan id in plan_id. "
              "Never invent a volume the user did not give.")

    def interpret(text: str, view: dict) -> Intent:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL, max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": INTENT_SCHEMA}},
            system=system,
            messages=[{"role": "user", "content": f"Bench:\n{json.dumps(view)}\n\nMessage:\n{text}"}],
        )
        if resp.stop_reason == "refusal":
            return Intent("unclear", note="I can't help with that request.")
        raw = next((b.text for b in resp.content if b.type == "text"), "{}")
        d = json.loads(raw)
        return Intent(**{k: d.get(k) for k in ("action", "vial_id", "liquid", "ml", "target", "plan_id")},
                      note=d.get("note") or "")

    return interpret


# ---------------------------------------------------------------- plan + validation (code, not model)

def build_plan(intent: Intent, view: dict, request: str) -> tuple[dict | None, str]:
    """Intent -> executor plan, or (None, reason). Resolves the vial from the liquid when not named."""
    vial = intent.vial_id
    if vial is None and intent.liquid:
        matches = [vid for vid, v in view["vials"].items() if v["liquid"] == intent.liquid]
        if not matches:
            return None, f"no {intent.liquid} vial on the bench"
        vial = max(matches, key=lambda vid: view["vials"][vid]["ml"])
    if vial is None:
        return None, "which vial? " + ", ".join(f"{vid} ({v['liquid']})" for vid, v in view["vials"].items())
    if vial not in view["vials"]:
        return None, f"vial {vial} is not on the bench"
    v = view["vials"][vial]
    target = intent.target or view["station_target"]
    ml = round(float(intent.ml), 2)
    plan = {
        "plan_id": uuid.uuid4().hex[:6],
        "request": request.strip()[:200],
        "steps": [
            {"skill": "pick", "args": {"vial_id": vial}},
            {"skill": "carry", "args": {"to": "station"}},
            {"skill": "pour", "args": {"target": target, "ml": ml, "liquid": v["liquid"]}},
            {"skill": "verify", "args": {"target": target, "expect_ml": ml, "tol": DEFAULT_TOL_ML}},
            {"skill": "carry", "args": {"to": "rack"}},
            {"skill": "place", "args": {"vial_id": vial, "slot": v["slot"]}},
        ],
    }
    return plan, ""


def validate_plan(plan: dict, view: dict) -> list[str]:
    """Problems with a plan against the bench as it is now; empty = valid. Also passed to the Executor."""
    problems, holding, poured = [], None, 0.0
    known = {"pick", "carry", "pour", "verify", "place"}
    for i, s in enumerate(plan.get("steps", [])):
        skill, args = s.get("skill"), s.get("args", {})
        where = f"step {i + 1} ({skill})"
        if skill not in known:
            problems.append(f"{where}: unknown skill")
        elif skill == "pick":
            if holding:
                problems.append(f"{where}: already holding {holding}")
            if args.get("vial_id") not in view["vials"]:
                problems.append(f"{where}: vial {args.get('vial_id')} not on the bench")
            holding = args.get("vial_id")
        elif skill == "pour":
            ml = args.get("ml")
            if not holding:
                problems.append(f"{where}: nothing in the gripper")
            elif not isinstance(ml, (int, float)) or not 0 < ml <= MAX_ML:
                problems.append(f"{where}: volume must be between 0 and {MAX_ML:g} mL")
            else:
                v = view["vials"].get(holding, {})
                if ml > v.get("ml", 0) - 0.5:
                    problems.append(f"{where}: {holding} has {v.get('ml', 0):g} mL; keep 0.5 mL in the vial")
                if args.get("liquid") != v.get("liquid"):
                    problems.append(f"{where}: {holding} holds {v.get('liquid')}, not {args.get('liquid')}")
                poured += ml
            if args.get("target") != view["station_target"]:
                problems.append(f"{where}: target {args.get('target')} is not the station tube")
        elif skill == "place":
            if args.get("vial_id") != holding:
                problems.append(f"{where}: not holding {args.get('vial_id')}")
            if args.get("slot") not in view["slots"]:
                problems.append(f"{where}: unknown slot {args.get('slot')}")
            holding = None
    if poured > view["tube_capacity_ml"]:
        problems.append(f"plan pours {poured:g} mL; the station tube holds {view['tube_capacity_ml']:g} mL")
    if holding:
        problems.append(f"plan ends still holding {holding}")
    return problems


def describe_plan(plan: dict) -> str:
    lines = []
    for i, s in enumerate(plan["steps"], 1):
        args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
        lines.append(f"{i}. {s['skill']}({args})")
    return "\n".join(lines)


def describe_report(rep: dict) -> str:
    lines = [f"Run {rep['run_id']}: {rep['status'].upper()}"]
    for s in rep["steps"]:
        mark = {"ok": "ok", "failed": "FAILED", "skipped": "skipped", "pending": "-"}[s["status"]]
        lines.append(f"- {s['skill']}: {mark}" + (f" ({s['reason']})" if s.get("reason") else ""))
    if rep.get("abort"):
        a = rep["abort"]
        lines.append(f"Aborted at step {a['step'] + 1}: {a['reason']}. Safe pose reached: {a['safe_pose_reached']}.")
    if rep.get("rejected"):
        lines.append("Rejected: " + "; ".join(rep["rejected"]))
    if rep.get("results"):
        lines.append("Results: " + ", ".join(f"{k}={v}" for k, v in rep["results"].items()))
    return "\n".join(lines)


# ---------------------------------------------------------------- the agent

HELP = ("I'm Laminar, a lab robot that pours measured volumes from vials into a tube, checks the result with "
        "a camera, and reports back.\n"
        "- 'what's on the bench?'\n- 'pour 2 mL of blue into tube1'\n"
        "- 'approve <id>' to run a plan I proposed (approvers only), 'cancel <id>'\n- 'runs' for recent results")


class LabAgent:
    def __init__(self, system: System | None = None, approvers: set[str] | None = None,
                 interpret: Callable[[str, dict], Intent] | None = None, executor_factory=None):
        from .executor import Executor
        self.system = system or make_system(time_scale=0)
        self.simulated = type(self.system.skills).__name__.startswith("Fake")  # nothing physical moves
        if approvers is None:
            approvers = {a.strip() for a in os.environ.get(APPROVERS_ENV, "").split(",") if a.strip()}
        self.approvers = approvers
        if interpret is None:
            interpret = claude_interpreter() if os.environ.get(PLANNER_ENV) == "claude" else parse_request
        self.interpret = interpret
        self.executor_factory = executor_factory or (lambda: Executor(
            self.system, validator=lambda plan, bench: validate_plan(plan, bench_view(self.system))))
        self.plans: dict[str, Pending] = {}
        self._lock = threading.Lock()   # one run at a time: there is one arm

    def can_approve(self, sender: str) -> bool:
        return sender in self.approvers or (self.simulated and not self.approvers)

    def handle(self, sender: str, text: str) -> str:
        view = bench_view(self.system)
        try:
            intent = self.interpret(text, view)
        except Exception as e:  # interpreter down (network, quota): fall back to the rule parser
            telemetry.log("agent.interpret_error", {"error": repr(e)})
            intent = parse_request(text, view)
        telemetry.log("agent.message", {"sender": sender, "text": text[:200], "intent": asdict(intent)})

        if intent.action == "help":
            return HELP
        if intent.action == "bench":
            return describe_bench(view)
        if intent.action == "unclear":
            return intent.note or HELP
        if intent.action == "runs":
            done = [p for p in self.plans.values() if p.report]
            if not done:
                return "No runs yet."
            return "\n\n".join(describe_report(p.report) for p in done[-3:])
        if intent.action == "cancel":
            p = self.plans.get(intent.plan_id or "")
            if not p or p.status != "pending":
                return f"No pending plan {intent.plan_id}."
            p.status = "cancelled"
            return f"Plan {intent.plan_id} cancelled. Nothing moved."
        if intent.action == "approve":
            return self.approve(sender, intent.plan_id or "")
        return self.propose(sender, text, intent, view)

    def propose(self, sender: str, text: str, intent: Intent, view: dict) -> str:
        plan, why = build_plan(intent, view, text)
        if plan is None:
            return f"I can't plan that: {why}."
        problems = validate_plan(plan, view)
        if problems:
            return "That plan fails the safety checks, so nothing will move:\n- " + "\n- ".join(problems)
        self.plans[plan["plan_id"]] = Pending(plan, sender)
        telemetry.log("agent.plan", {"plan_id": plan["plan_id"], "sender": sender, "plan": plan})
        who = ("you can approve it" if self.can_approve(sender)
               else "an approver at the lab has to approve it")
        sim = " (simulated bench)" if self.simulated else ""
        return (f"Plan {plan['plan_id']}{sim}, checked against the bench:\n{describe_plan(plan)}\n"
                f"Reply 'approve {plan['plan_id']}' to run it ({who}), or 'cancel {plan['plan_id']}'.")

    def approve(self, sender: str, plan_id: str) -> str:
        p = self.plans.get(plan_id)
        if not p:
            return f"No plan {plan_id}."
        if p.status != "pending":
            return f"Plan {plan_id} is already {p.status}."
        if not self.can_approve(sender):
            telemetry.log("agent.approve_denied", {"plan_id": plan_id, "sender": sender})
            return f"You are not on the approver list, so plan {plan_id} stays pending. Nothing moved."
        if not self._lock.acquire(blocking=False):
            return "The arm is busy with another run; approve again when it finishes."
        try:
            p.status = "running"
            report: RunReport = self.executor_factory().run(p.plan, approved_by=sender)
            p.report = report.to_dict()
            p.status = report.status
        finally:
            self._lock.release()
        return describe_report(p.report)
