"""Wire voice into Laminar: the team repo's planner, validator, executor and camera pour.

    mic ─► voice.VoiceLoop ─► backend.plan() ─► backend.validate() ─► spoken read-back
                                                                        │ "approve"
                                                                        ▼
                    backend.run(plan, emit) ──► EventBus ──► VoiceAnnouncer ─► speaker
                                                   └──────► console log

Three backends, same voice on top (pick with --backend):

  fake   no repo needed. Fake planner, validator and robot.
  om     Om's code (software/laminar): lab_agent.parse_request (or Claude with --claude) + build_plan,
         validate_plan, and the Executor on make_system() (fake arm today, the real one once Om wires
         it). Progress comes from telemetry events (executor.step, pour.sample, executor.abort), so
         nothing in Om's code changes. Say: "pour 2 ml of blue into the tube".
  arm    the REAL arm: Avery's robotics/transfer.py moves (lift, shift, lower, camera pour, back)
         with Om's pour_motor6 camera pour. Say: "pour 1 ml of blue into column C".
         Needs, in the repo:
           - robotics/transfer.py: CameraPour.pour() / pour_ml() take an on_tick callback
           - robotics/layout.yaml: `vials:` says which liquid each vial holds; routes measured
           - sense/live_boxes.json: one box per compartment, drawn with `cd sense && python live_view.py
             --new-boxes` (A, B, C, D left to right); the level model reads them, no calibration
           - arm_server.py running, and the arm already holding the vial (the grab is a recorded
             step, not part of transfer.py)
         --dry-run plans and prints the moves without moving the arm or opening the camera.

"stop" by voice: arm backend tilts the vial upright at once (the pour loop's on_tick raises, which
pour_motor6 treats as "stopped by operator") and the arm then HOLDS where it is: it does not carry
back on its own after a stop. A move already running finishes first; the hardware e-stop is the real
stop. om backend: Executor.cancel(), which takes effect between steps.

Run from anywhere (--repo finds the laminar checkout; default: a parent folder of this file or ~/laminar):
  python integration.py --mock                                  # fake everything, typed
  python integration.py --mock --backend om                     # Om's planner + executor, typed
  python integration.py --mock --backend arm --dry-run          # real layout + planning, no motion
  python integration.py --backend arm --cam 2                   # real mic, real arm
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from voice import (Listener, PourAnnouncer, RobotHooks, Speaker, VoiceLoop,
                   extract_column, extract_ml)

LIQUIDS = ("red", "blue", "green", "yellow", "orange", "purple", "pink", "clear")


# ---------------------------------------------------------------- event bus

class EventBus:
    """Tiny publish/subscribe so the robot code never needs to know who listens."""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self, fn):
        with self._lock:
            self._subs.append(fn)

    def emit(self, event: str, data: dict | None = None):
        for fn in list(self._subs):
            try:
                fn(event, data or {})
            except KeyboardInterrupt:
                raise
            except Exception as e:                 # a broken listener must never stop the robot
                print(f"[bus] listener error on {event}: {e}")


class VoiceAnnouncer:
    """Turns robot events into speech.

    step_start    {"skill": "pick", "vial_id": "V1", ...}  -> "Picking vial V1."
    pour_progress {"column": "A", "label": "Column A", "cmd": obj with .phase .ml .reason}
    step_failed   {"skill": "...", "reason": "..."}        -> urgent alert
    job_done      {"report": {"pours": [{"label", "ml"}]}} -> spoken summary
    job_aborted   {"reason": "..."}                        -> urgent alert
    """

    def __init__(self, speaker: Speaker):
        self.sp = speaker
        self._pour: dict[str, PourAnnouncer] = {}

    def step_line(self, d: dict) -> str | None:
        skill, vial = d.get("skill"), _spoken_vial(d.get("vial_id", ""))
        if skill == "pick":
            return f"Picking {vial}."
        if skill == "carry":
            return "Carrying back to the rack." if d.get("to") == "rack" else "Carrying to the station."
        if skill == "pour":
            return f"Pouring into {d.get('spoken_target') or 'column ' + str(d.get('target'))}."
        if skill == "verify":
            return "Checking the volume."
        if skill == "place":
            return f"Putting {vial} back."
        return None

    def __call__(self, event: str, d: dict):
        if event == "step_start":
            line = self.step_line(d)
            if line:
                self.sp.say(line)
        elif event == "pour_progress":
            col, cmd = d["column"], d["cmd"]
            if cmd.phase != "abort":                 # job_aborted says why, once
                self._pour.setdefault(col, PourAnnouncer(self.sp, col, d.get("label"))).update(cmd)
        elif event == "step_failed":
            self.sp.say(f"Problem: {_sentence(d.get('reason', ''))}", urgent=True)
        elif event == "job_aborted":
            reason = _sentence(d.get("reason", ""))
            self.sp.say(f"Job stopped. {reason}" if reason else "Job stopped.", urgent=True)
        elif event == "job_done":
            self._pour.clear()
            self.sp.say(spoken_summary(d.get("report", {})))


def _sentence(text: str) -> str:
    text = re.sub(r"\s*\([^)]*\)", "", str(text)).strip().rstrip(".")   # drop "(+0.40 mL)" etc.
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _spoken_vial(vial_id: str) -> str:
    m = re.fullmatch(r"vial_(\w)", vial_id or "")
    return f"vial {m.group(1)}" if m else (f"vial {vial_id}" if vial_id else "the vial")


def spoken_summary(report: dict) -> str:
    """Spoken report built from measured numbers only."""
    parts = [f"{p['label'][:1].upper() + p['label'][1:]}, {p['ml']:.1f} millilitres"
             for p in report.get("pours", [])
             if p.get("ml") is not None]
    head = "Job finished, but check it." if report.get("problems") else "Job complete."
    return (head + " " + "; ".join(parts) + ".") if parts else head


def phase_for(fill: float) -> str:
    """Fill fraction of target -> the phase names PourAnnouncer speaks (same as PourGuide's)."""
    return "full" if fill < 0.5 else "slow" if fill < 0.75 else "slower" if fill < 0.9 else "final"


# ---------------------------------------------------------------- hooks

def make_hooks(backend, bus: EventBus, log=print) -> RobotHooks:
    def make_plan(text: str) -> dict:
        out = backend.plan(text)
        if "question" in out:
            return out
        errors = backend.validate(out["plan"])
        if errors:
            log("plan_rejected", {"text": text, "errors": errors})
            return {"question": f"I can't run that: {_sentence(errors[0])} What should I do instead?"}
        log("plan_ok", {"text": text, "plan": out["plan"]})
        return out

    def run_plan(plan: dict):
        def go():
            try:
                backend.run(plan, bus.emit)
            except Exception as e:
                bus.emit("job_aborted", {"reason": str(e)})
        threading.Thread(target=go, daemon=True).start()

    return RobotHooks(make_plan=make_plan, run_plan=run_plan, stop=backend.abort,
                      status=backend.status_text, log=lambda ev, d: log(ev, d))


class Backend:
    """What every backend provides. plan() returns {"plan": {...}} or {"question": "..."}; plans use
    the executor's shape: {"plan_id", "request", "steps": [{"skill", "args": {...}}]}."""

    def __init__(self):
        self._stop = threading.Event()
        self._status = "Idle, ready."

    def abort(self):
        self._stop.set()

    def status_text(self) -> str:
        return self._status


# ---------------------------------------------------------------- backend: fake

class FakeBackend(Backend):
    CAPACITY_ML = 8.0

    def plan(self, text):
        ml, col = extract_ml(text), extract_column(text)
        if ml is None or col is None:
            return {"question": "How many millilitres, and into which column, A to D?"}
        liquid = next((c for c in LIQUIDS if c in text.lower()), "blue")
        vial = "vial_" + {"red": "B"}.get(liquid, "A")
        return {"plan": {"plan_id": uuid.uuid4().hex[:6], "request": text, "steps": [
            {"skill": "pick", "args": {"vial_id": vial}},
            {"skill": "carry", "args": {"to": "station"}},
            {"skill": "pour", "args": {"target": col, "ml": ml, "liquid": liquid}},
            {"skill": "carry", "args": {"to": "rack"}},
            {"skill": "place", "args": {"vial_id": vial}}]}}

    def validate(self, plan):
        errs = []
        for s in plan["steps"]:
            a = s["args"]
            if s["skill"] == "pour":
                if a["target"] not in "ABCD":
                    errs.append(f"column {a['target']} is not in use")
                if a["ml"] > self.CAPACITY_ML:
                    errs.append(f"{a['ml']:g} millilitres would overflow column {a['target']}")
        return errs

    def run(self, plan, emit):
        self._stop.clear()
        pours = []
        for step in plan["steps"]:
            a = step["args"]
            if self._stop.is_set():
                self._status = "Stopped."
                emit("job_aborted", {"reason": "stopped by the operator"})
                return
            emit("step_start", {"skill": step["skill"], **a})
            self._status = f"Running {step['skill']}."
            if step["skill"] == "pour":
                for frac in (0.3, 0.55, 0.8, 0.95):
                    time.sleep(0.8)
                    if self._stop.is_set():
                        break
                    cmd = SimpleNamespace(phase=phase_for(frac), ml=a["ml"] * frac, reason="")
                    self._status = f"Pouring column {a['target']}, {int(frac * 100)} percent."
                    emit("pour_progress", {"column": a["target"], "cmd": cmd})
                if not self._stop.is_set():
                    emit("pour_progress", {"column": a["target"],
                                           "cmd": SimpleNamespace(phase="stop", ml=a["ml"], reason="")})
                    pours.append({"label": f"column {a['target']}", "ml": a["ml"]})
            else:
                time.sleep(1.0)
        self._status = "Idle, ready."
        emit("job_done", {"report": {"pours": pours}})


# ---------------------------------------------------------------- backend: om

class OmBackend(Backend):
    """Om's lab agent planner + validator + Executor, unchanged. Events come from telemetry."""

    def __init__(self, repo: Path, claude: bool = False, time_scale: float = 1.0):
        super().__init__()
        sys.path.insert(0, str(repo / "software"))
        from laminar import lab_agent as LA, telemetry
        from laminar.executor import Executor
        from laminar.hardware import make_system
        self.LA, self.Executor = LA, Executor
        self.system = make_system(time_scale=time_scale)
        self.interpret = LA.claude_interpreter() if claude else LA.parse_request
        self.ex = None
        self._emit = None
        self._target_ml = 0.0
        telemetry.add_forwarder(self._on_telemetry)

    def view(self):
        return self.LA.bench_view(self.system)

    def plan(self, text):
        view = self.view()
        try:
            intent = self.interpret(text, view)
        except Exception as e:               # Claude unreachable: same fallback as lab_agent
            print(f"[om] interpreter failed ({e}), using the rule parser")
            intent = self.LA.parse_request(text, view)
        if intent.action == "bench":
            return {"question": self.LA.describe_bench(view).replace("\n", " ")}
        if intent.action != "plan":
            return {"question": intent.note or "Tell me how many millilitres of which liquid to pour."}
        plan, why = self.LA.build_plan(intent, view, text)
        if plan is None:
            return {"question": f"I can't plan that: {_sentence(why)}"}
        for s in plan["steps"]:              # say "the tube", not "column tube1"
            if s["skill"] == "pour":
                s["args"]["spoken_target"] = "the tube"
        return {"plan": plan}

    def validate(self, plan):
        return self.LA.validate_plan(_strip_spoken(plan), self.view())

    def run(self, plan, emit):
        self._emit, self._status = emit, "Running."
        self.ex = self.Executor(self.system,
                                validator=lambda p, bench: self.LA.validate_plan(p, self.view()))
        rep = self.ex.run(_strip_spoken(plan), approved_by="voice").to_dict()
        self._status = "Idle, ready."
        if rep["status"] == "done":
            res = rep.get("results", {})
            ml = res.get("measured_ml", res.get("dispensed_ml"))
            emit("job_done", {"report": {"pours": [{"label": "the tube", "ml": ml}]}})
        else:
            why = (rep.get("abort") or {}).get("reason") or "; ".join(rep.get("rejected", []))
            emit("job_aborted", {"reason": why})

    def abort(self):
        if self.ex:
            self.ex.cancel("stopped by voice")

    def _on_telemetry(self, rec: dict):
        emit, ev = self._emit, rec.get("event")
        if emit is None:
            return
        if ev == "executor.step":
            if rec["skill"] == "pour":
                self._target_ml = float(rec["args"].get("ml") or 0.0)
            spoken = {"spoken_target": "the tube"} if rec["skill"] == "pour" else {}
            emit("step_start", {"skill": rec["skill"], **rec["args"], **spoken})
            self._status = f"Running {rec['skill']}."
        elif ev == "pour.sample" and rec.get("ml") is not None and self._target_ml:
            fill = rec["ml"] / self._target_ml
            self._status = f"Pouring, {int(fill * 100)} percent."
            emit("pour_progress", {"column": "tube", "label": "The tube",
                                   "cmd": SimpleNamespace(phase=phase_for(fill), ml=rec["ml"], reason="")})
        elif ev == "pour.end" and rec.get("state") == "done":
            emit("pour_progress", {"column": "tube", "label": "The tube",
                                   "cmd": SimpleNamespace(phase="stop", ml=rec.get("ml") or 0.0, reason="")})


def _strip_spoken(plan: dict) -> dict:
    """Om's executor checks args exactly: drop the voice-only spoken_target before it sees the plan."""
    return {**plan, "steps": [{**s, "args": {k: v for k, v in s["args"].items() if k != "spoken_target"}}
                              for s in plan["steps"]]}


# ---------------------------------------------------------------- backend: arm (real)

class ArmBackend(Backend):
    """robotics/transfer.py's moves + camera pour, driven by voice. One pour per plan."""

    def __init__(self, repo: Path, cam: str = "0", boxes: str | None = None, dry_run: bool = False,
                 max_tilt: float = 85.0, view: bool = False):
        super().__init__()
        for sub in ("sense", "software", "robotics"):
            sys.path.insert(0, str(repo / sub))
        import transfer as T
        self.T, self.repo, self.cam, self.dry_run = T, repo, cam, dry_run
        self.boxes = boxes                                   # None = sense/live_boxes.json
        self.max_tilt, self.view = max_tilt, view
        self.layout = T.load_layout()
        self.cfg = self.layout.get("pour") or {}
        self.columns = self.cfg.get("columns", {})           # compartment -> camera column
        self.vials = self.layout.get("vials") or {}          # vial -> liquid (new in layout.yaml)
        if not self.vials:
            print("[arm] layout.yaml has no `vials:` (which liquid each vial holds): "
                  "say the vial, e.g. 'pour 1 ml from vial A into column C'")

    # ---- planning

    def plan(self, text):
        t = text.lower()
        ml, col = extract_ml(text), extract_column(text)
        if ml is None or col is None:
            return {"question": "How many millilitres, and into which column, A to D?"}
        m = re.search(r"\bvial\s+(a|b|c|d)\b", t)
        vial = f"vial_{m.group(1).upper()}" if m else None
        liquid = next((c for c in LIQUIDS if re.search(rf"\b{c}\b", t)), None)
        if vial is None and liquid:
            matches = [v for v, liq in self.vials.items() if liq == liquid]
            if len(matches) != 1:
                return {"question": f"Which vial has the {liquid}? Say vial A, B, C or D."}
            vial = matches[0]
        if vial is None:
            return {"question": "Which vial? Say the colour, or vial A, B, C or D."}
        liquid = liquid or self.vials.get(vial)
        well = next((w for w, c in self.columns.items() if c == col), None)
        args = {"vial_id": vial, "compartment": well, "target": col, "ml": ml,
                "liquid": liquid, "spoken_target": f"column {col}"}
        return {"plan": {"plan_id": uuid.uuid4().hex[:6], "request": text,
                         "steps": [{"skill": "pour", "args": args}]}}

    def validate(self, plan):
        errs = []
        for s in plan["steps"]:
            a = s["args"]
            if a["compartment"] is None:
                errs.append(f"column {a['target']} is not one of the compartments in layout.yaml")
                continue
            if self.vials and a["vial_id"] not in self.vials:
                errs.append(f"{_spoken_vial(a['vial_id'])} is not in layout.yaml")
            elif self.vials and a["liquid"] and self.vials[a["vial_id"]] != a["liquid"]:
                errs.append(f"{_spoken_vial(a['vial_id'])} holds {self.vials[a['vial_id']]}, "
                            f"not {a['liquid']}")
            try:
                self.T.route(self.layout, a["vial_id"], a["compartment"])
            except (KeyError, ValueError) as e:
                errs.append(f"the arm can't go there yet: {e}")
            cap = self._capacity_ml()
            if not 0 < a["ml"] <= (cap or 0):
                errs.append(f"{a['ml']:g} millilitres is more than one channel holds ({cap:g})")
        return errs

    def _capacity_ml(self) -> float | None:
        return self.T.capacity_ml(self.layout)

    # ---- running (same order and checks as transfer.main)

    def run(self, plan, emit):
        T, M = self.T, self.T.M
        self._stop.clear()
        a = plan["steps"][0]["args"]
        vial, well, col, ml = a["vial_id"], a["compartment"], a["target"], a["ml"]
        dxy = T.route(self.layout, vial, well)
        lift = float(self.layout.get("lift_mm", 150))
        lower = float(self.layout.get("lower_mm", 110))
        direction = T.pour_direction(self.cfg, None)
        client = T.ArmClient(dry_run=self.dry_run)
        q0 = client.current_joints() if not self.dry_run else _dry_joints()
        out = M.plan_transfer(q0, dxy, lift_mm=lift, lower_mm=lower)
        back = M.plan_return(out[-1].final_joints, dxy, lift_mm=lift, lower_mm=lower)
        room = T.roll_room_deg(out[-1].final_joints[6], direction)
        if room < 20:
            raise RuntimeError(f"only {room:.0f} degrees of wrist roll left: re-grab with the roll nearer zero")
        for p in out + back:
            print("  " + p.summary())

        camera = None if self.dry_run else T.CameraPour(self.cam, col, T.capacity_ml(self.layout),
                                                         boxes=self.boxes)  # checked before moving
        label = f"Column {col}"
        try:
            emit("step_start", {"skill": "carry", "to": "station"})
            self._status = f"Carrying {_spoken_vial(vial)} to column {col}."
            for p in out:
                if self._stop.is_set():
                    return self._stopped(emit)
                client.run(p)
            if self._stop.is_set():
                return self._stopped(emit)

            emit("step_start", {"skill": "pour", **a})
            if camera is None:
                print(f"  DRY RUN: camera pour of {ml:g} mL into column {col} skipped")
                added, result = ml, SimpleNamespace(ok=True, result="done", reason="dry run")
            else:
                def tick(rec):
                    if self._stop.is_set():
                        raise KeyboardInterrupt          # pour_motor6: "stopped by operator", tilts back
                    if rec.get("final"):
                        return
                    self._status = f"Pouring column {col}, {int(rec['fill'] * 100)} percent."
                    emit("pour_progress", {"column": col, "label": label,
                                           "cmd": SimpleNamespace(phase=rec["phase"], ml=rec["ml"],
                                                                  reason="")})
                result, added = camera.pour_ml(direction, ml, self.max_tilt, view=self.view,
                                               on_tick=tick)
            if self._stop.is_set():
                return self._stopped(emit)                # upright now; hold, don't carry back
            if result.ok:
                emit("pour_progress", {"column": col, "label": label,
                                       "cmd": SimpleNamespace(phase="stop", ml=added or 0.0, reason="")})
            else:
                emit("step_failed", {"skill": "pour", "reason": result.reason})
            problems = []
            if camera is not None and result.ok and a.get("liquid"):
                seen = camera.tracker.read(col)
                if seen.conf >= 0.5 and seen.color not in ("none", "clear", a["liquid"]):
                    problems.append(f"column {col} looks {seen.color}, expected {a['liquid']}")
                    emit("step_failed", {"skill": "verify", "reason": problems[-1]})

            emit("step_start", {"skill": "carry", "to": "rack"})
            self._status = "Carrying back."
            for p in back:                               # transfer.py carries back after any pour result
                client.run(p)
        finally:
            if camera:
                camera.close()
        self._status = "Idle, ready."
        if result.ok:
            emit("job_done", {"report": {"pours": [{"label": f"column {col}", "ml": added}],
                                         "problems": problems}})
        else:
            emit("job_aborted", {"reason": f"the vial is back. Column {col} did not get its "
                                           f"{ml:g} millilitre{'' if ml == 1 else 's'}"})

    def _stopped(self, emit):
        self._status = "Stopped. Holding."
        emit("job_aborted", {"reason": "stopped by voice. The vial is upright and the arm is holding "
                                       "where it is"})


def _dry_joints() -> dict[int, float]:
    """No arm_server in a dry run: plan from the last commanded pose if one is saved, else a safe guess."""
    try:
        st = json.loads((Path(sys.modules["arm_client"].STATUS_FILE)).read_text())
        return {i: float(st["command"][f"J{i}"]) for i in range(1, 7)}
    except Exception:
        return {1: 0.0, 2: 30.0, 3: 60.0, 4: 0.0, 5: 30.0, 6: 0.0}


# ---------------------------------------------------------------- main

def find_repo(start: Path) -> Path | None:
    for d in [start, *start.parents, Path.home() / "laminar"]:
        if (d / "robotics" / "transfer.py").exists() and (d / "software" / "laminar").is_dir():
            return d
    return None


def build(mock: bool, wake: str | None, backend: Backend):
    client = None
    if not mock:
        from cartesia import Cartesia
        client = Cartesia(api_key=os.environ["CARTESIA_API_KEY"])
    bus = EventBus()
    sp = Speaker(client, mock=mock)
    sp.precache()
    bus.subscribe(VoiceAnnouncer(sp))
    bus.subscribe(lambda ev, d: print(f"  [event] {ev} {d if ev != 'pour_progress' else ''}"))
    hooks = make_hooks(backend, bus, log=lambda ev, d: print(f"  [log] {ev}: {d}"))
    loop = VoiceLoop(sp, Listener(client, sp, mock=mock), hooks, wake=wake)
    return loop, bus


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="type instead of speaking, no Cartesia")
    ap.add_argument("--wake", default=None, help="require a wake word, e.g. --wake laminar")
    ap.add_argument("--backend", choices=["fake", "om", "arm"], default="fake")
    ap.add_argument("--repo", default=None, help="laminar checkout (default: found automatically)")
    ap.add_argument("--claude", action="store_true", help="om: read requests with Claude")
    ap.add_argument("--cam", default="0", help="arm: side camera index")
    ap.add_argument("--boxes", default=None, help="arm: side-camera boxes (default sense/live_boxes.json)")
    ap.add_argument("--max-tilt", type=float, default=85.0, help="arm: max wrist roll, degrees")
    ap.add_argument("--view", action="store_true", help="arm: show the camera window during the pour")
    ap.add_argument("--dry-run", action="store_true", help="arm: plan and print, don't move or pour")
    a = ap.parse_args()

    backend: Backend
    if a.backend == "fake":
        backend = FakeBackend()
    else:
        repo = Path(a.repo).expanduser().resolve() if a.repo else find_repo(Path(__file__).resolve().parent)
        if repo is None:
            sys.exit("Can't find the laminar repo: pass --repo /path/to/laminar")
        backend = (OmBackend(repo, claude=a.claude) if a.backend == "om" else
                   ArmBackend(repo, a.cam, a.boxes, dry_run=a.dry_run, max_tilt=a.max_tilt, view=a.view))
    loop, bus = build(a.mock, a.wake, backend)
    example = {"fake": "Pour two milliliters of blue into column A",
               "om": "Pour 2 ml of blue into the tube",
               "arm": "Pour 1 ml of blue into column C"}[a.backend]
    print(f'Say: "{example}", then "approve". "stop" any time.')
    loop.run()


if __name__ == "__main__":
    main()
