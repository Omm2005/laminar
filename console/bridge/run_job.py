"""Run one Convex-approved job through robotics/transfer.py's pieces.

This is the replacement for pour/robot_client.py (now legacy). It never modifies
anything in robotics/ — it imports their functions, assembles a transfer the same
way transfer.main() does, and runs it. Every Convex hook (step_start, step_end,
pushLevels, pour_record, setStatus) is wrapped in try/except so a cloud outage
or schema mismatch can never derail the robot.

With TIGER_DATABASE_URL set, each job is also a Tiger run (software/laminar/tiger.py RunLogger):
an arm_runs row, every hook below as a laminar_events row tagged with the job id, and the 100 Hz
arm trace while the job runs. The console's Runs page reads it back for trajectory analysis.

Convex job fields consumed:
    jobId      (jobs._id)
    vial       (number 1..) -> "vial_A", "vial_B", ...
    channel    (number 1..) -> "well_1", "well_2", ... (via layout.yaml.pour.columns)
    targetMl   (number)      -> mL to ADD to the channel (CameraPour.pour_ml: current level + targetMl,
                                using the camera calibration's capacity_ml)

Usage:
    python console/bridge/run_job.py --cam 0 [--once] [--poll 2] [--dry-run] [--no-pour]
                                       [--calib ../sense/level_calib.json]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMINAR = HERE.parent.parent
ROBOTICS = LAMINAR / "robotics"
sys.path.insert(0, str(ROBOTICS))
sys.path.insert(0, str(LAMINAR / "software"))
sys.path.insert(0, str(LAMINAR / "sense"))

from convex import ConvexClient  # noqa: E402
from laminar.tiger import RunLogger                          # noqa: E402

# Their code — imported unchanged.
import motion as M                                           # noqa: E402
from arm_client import ArmClient                             # noqa: E402
from transfer import (                                       # noqa: E402
    load_layout, route, pour_direction, camera_column, roll_room_deg,
    CameraPour, pour_fixed,
)

ENV_FILE = LAMINAR / "console" / ".env.local"


def read_convex_url() -> str:
    for v in ("CONVEX_URL", "VITE_CONVEX_URL"):
        if os.environ.get(v):
            return os.environ[v]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            m = re.match(r"^\s*(CONVEX_URL|VITE_CONVEX_URL)\s*=\s*(.*)$", line)
            if m:
                return m.group(2).strip().strip('"').strip("'")
    raise SystemExit("CONVEX_URL not set and console/.env.local missing")


def vial_name(n: int) -> str:
    return f"vial_{chr(ord('A') + int(n) - 1)}"


def well_name(n: int) -> str:
    return f"well_{int(n)}"


class CloudHooks:
    """Thin, failure-tolerant wrapper around Convex mutations. One bad push doesn't abort the job."""

    def __init__(self, convex: ConvexClient, job_id: str, tiger: RunLogger | None = None):
        self.c = convex
        self.job_id = job_id
        self.tiger = tiger

    def _safe(self, name: str, path: str, args: dict):
        if self.tiger:
            self.tiger.event(f"job.{name}", {k: v for k, v in args.items() if k != "jobId"})
        try:
            return self.c.mutation(path, args)
        except Exception as e:
            print(f"[cloud/{name}] skipped: {e!r}")

    def set_status(self, status: str, step: str | None = None):
        patch: dict = {"jobId": self.job_id, "status": status}
        if step is not None:
            patch["currentStep"] = step
        self._safe("setStatus", "jobs:setStatus", patch)

    def step_start(self, name: str):
        return self._safe("stepStart", "steps:start", {"jobId": self.job_id, "name": name})

    def step_end(self, step_id, ok: bool, reason: str | None = None):
        if step_id is None:
            return
        args: dict = {"stepId": step_id, "ok": ok}
        if reason:
            args["reason"] = reason
        self._safe("stepEnd", "steps:end", args)

    def push_level(self, ml: float, channel: int | None = None):
        self._safe("pushLevels", "robot:pushLevels", {
            "jobId": self.job_id,
            "samples": [{"t": time.time() * 1000.0, "ml": float(ml), "channel": channel}],
        })

    def pour_record(self, target_ml, poured_ml, onset_deg, pour_deg, mlPerS):
        self._safe("pourRecord", "pours:record", {
            "jobId": self.job_id,
            "targetMl": float(target_ml), "pouredMl": float(poured_ml),
            "onsetDeg": float(onset_deg), "pourDeg": float(pour_deg),
            "measuredMlPerS": float(mlPerS),
        })

    def event(self, kind: str, message: str):
        self._safe("event", "events:log", {
            "kind": kind, "source": "run_job", "jobId": self.job_id, "message": message,
        })

    def is_stopped(self, convex: ConvexClient) -> bool:
        try:
            s = convex.query("control:stopRequested", {})
            return bool(s and s.get("stop"))
        except Exception:
            return False


def run_one_job(convex: ConvexClient, job: dict, a: argparse.Namespace) -> None:
    job_id = job["jobId"]
    vial = vial_name(int(job["vial"]))
    comp = well_name(int(job["channel"]))
    target_ml = float(job["targetMl"])
    hooks = CloudHooks(convex, job_id)
    hooks.event("info", f"run_job picked up {target_ml:.2f} mL / {vial} -> {comp}")
    hooks.set_status("running", "plan")

    layout = load_layout()
    try:
        dxy = route(layout, vial, comp)
    except (KeyError, ValueError) as e:
        hooks.set_status("failed")
        hooks.event("error", f"layout: {e}")
        return

    cfg = layout.get("pour") or {}
    lift_mm = float(layout.get("lift_mm", 100))
    lower_mm = float(layout.get("lower_mm", 60))
    direction = pour_direction(cfg)
    column = camera_column(cfg, comp)

    client = ArmClient(dry_run=a.dry_run)
    try:
        q0 = client.current_joints()
    except RuntimeError as e:
        hooks.set_status("failed")
        hooks.event("error", str(e))
        return
    try:
        out = M.plan_transfer(q0, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
        back = M.plan_return(out[-1].final_joints, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
    except M.PlanError as e:
        hooks.set_status("failed")
        hooks.event("error", f"plan: {e}")
        return

    roll = out[-1].final_joints[6]
    if roll_room_deg(roll, direction) < 20 and not a.dry_run:
        hooks.set_status("failed")
        hooks.event("error", "insufficient wrist-roll clearance; re-grab with roll near 0")
        return

    # without a camera there is no pour: refuse rather than carry the vial out and back and call it done
    if a.cam is None and not a.no_pour and not a.dry_run:
        hooks.set_status("failed")
        hooks.event("error", "no side camera: start run_job with --cam <index> (or --no-pour for moves only)")
        return

    # open the camera BEFORE any motion so a bad camera never strands the tube
    camera: CameraPour | None = None
    if a.cam is not None and not a.dry_run:
        try:
            camera = CameraPour(a.cam, a.calib, column)
        except (RuntimeError, ImportError) as e:
            hooks.set_status("failed")
            hooks.event("error", f"camera: {e}")
            return

    # the Tiger run starts with the first move, so a job refused above leaves no half-open run behind
    tiger = None
    if not a.dry_run:
        try:
            tiger = RunLogger.start(job_id, name=f"{target_ml:.2f} mL {vial} -> {comp}",
                                    meta={"vial": vial, "well": comp, "target_ml": target_ml, "source": "run_job"})
        except Exception as e:
            print(f"[tiger] run not logged: {e!r}")
    hooks.tiger = tiger
    overall_ok = True
    end_status = "done"
    try:
        for p in out:
            if hooks.is_stopped(convex):
                raise RuntimeError("e-stop active")
            step_id = hooks.step_start(p.label)
            hooks.set_status("running", p.label)
            ok, reason = True, None
            try:
                client.run(p)
            except Exception as e:
                ok, reason = False, str(e)
                overall_ok = False
                end_status = "failed"
            hooks.step_end(step_id, ok, reason)
            if not ok:
                raise RuntimeError(reason or "move failed")

        if camera:
            step_id = hooks.step_start("pour")
            hooks.set_status("pouring", "pour")
            r = None
            try:
                # pour targetMl MORE on top of what the channel holds; refuses overfill / unreadable column
                r, added_ml = camera.pour_ml(direction, target_ml, 85.0, view=False)
            except Exception as e:
                hooks.step_end(step_id, False, str(e))
                hooks.event("error", f"pour: {e}")
                overall_ok, end_status = False, "failed"
            else:
                # a pour that did not finish (not started, aborted) never tilted or stopped early: say why
                hooks.step_end(step_id, bool(r.ok), f"{r.result}: {r.reason}")
                if not r.ok:
                    hooks.event("error", f"pour {r.result} at {r.max_tilt_deg:.0f} deg tilt: {r.reason}")
                    overall_ok, end_status = False, "failed"
                try:
                    hooks.pour_record(
                        target_ml=target_ml, poured_ml=added_ml or 0.0,
                        onset_deg=0.0,                     # PourResult has no onset angle
                        pour_deg=r.max_tilt_deg,
                        mlPerS=(added_ml or 0.0) / r.duration_s if r.duration_s else 0.0,
                    )
                except Exception as e:
                    hooks.event("warn", f"pour_record skipped: {e}")

        for p in back:
            if hooks.is_stopped(convex):
                raise RuntimeError("e-stop active during return")
            step_id = hooks.step_start(p.label)
            hooks.set_status("running", p.label)
            ok, reason = True, None
            try:
                client.run(p)
            except Exception as e:
                ok, reason = False, str(e)
                overall_ok = False
                end_status = "failed"
            hooks.step_end(step_id, ok, reason)
            if not ok:
                break
    except Exception as e:
        traceback.print_exc()
        hooks.event("error", str(e))
        end_status = "estop" if "e-stop" in str(e).lower() else "failed"
        overall_ok = False
    finally:
        if camera:
            try:
                camera.close()
            except Exception:
                pass
        hooks.set_status(end_status)
        hooks.event("info" if overall_ok else "error", f"job done, status={end_status}")
        if tiger:
            print(f"[tiger] {tiger.finish(end_status)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="plan only; arm_server optional")
    ap.add_argument("--cam", default=None, help="side-camera index for the pour (required unless --no-pour)")
    ap.add_argument("--no-pour", action="store_true", help="run the moves only, no pour (jobs report no pour)")
    ap.add_argument("--calib", default=str(LAMINAR / "sense" / "level_calib.json"))
    a = ap.parse_args()

    url = read_convex_url()
    print(f"run_job: convex @ {url}")
    convex = ConvexClient(url)

    try:
        while True:
            try:
                job = convex.query("jobs:nextForRobot", {})
            except Exception as e:
                print(f"[poll] query failed: {e!r}; retrying ...")
                time.sleep(a.poll)
                continue
            if job is None:
                time.sleep(a.poll)
                continue
            print(f"\n== job {job['jobId']} ({job['targetMl']:.2f} mL ch{int(job['channel'])}/v{int(job['vial'])}) ==")
            run_one_job(convex, job, a)
            if a.once:
                break
    except KeyboardInterrupt:
        print()
        print("run_job: stopped")


if __name__ == "__main__":
    main()
