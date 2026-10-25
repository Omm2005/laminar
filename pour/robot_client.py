"""Bridges pour/* to the Convex console backend.

Polls jobs.nextForRobot; for each approved job, drives the plan's primitives one at a
time (same subprocess pattern as pour/run_task.py), streams telemetry while running,
records the final pour by tailing pour/logs/pours.jsonl, and honors the e-stop.

Usage:
  python pour/robot_client.py [--poll 2] [--dry-run] [--keep-server] [--level auto|manual|opencv]

Env:
  CONVEX_URL  deployment URL. If unset, parsed from ../console/.env.local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMINAR = HERE.parent
TELEOP = LAMINAR / "teleop"
CONSOLE_ENV = LAMINAR / "console" / ".env.local"
POURS_LOG = HERE / "logs" / "pours.jsonl"
VENV_PY = LAMINAR / ".venv" / "Scripts" / "python.exe"
sys.path.insert(0, str(HERE))
import arm_io

from convex import ConvexClient

TELEM_HZ = 10.0


def read_convex_url() -> str:
    url = os.environ.get("CONVEX_URL") or os.environ.get("VITE_CONVEX_URL")
    if url:
        return url
    if CONSOLE_ENV.exists():
        for line in CONSOLE_ENV.read_text().splitlines():
            m = re.match(r"^\s*(CONVEX_URL|VITE_CONVEX_URL)\s*=\s*(.*)\s*$", line)
            if m:
                return m.group(2).strip().strip('"').strip("'")
    raise SystemExit(
        "CONVEX_URL not set and ../console/.env.local not found. "
        "Run `npx convex dev` in console/ first."
    )


def read_last_pour() -> dict | None:
    """Last line of pours.jsonl as a dict, or None if the file is missing or empty."""
    if not POURS_LOG.exists():
        return None
    last = None
    for line in POURS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            last = line
    if last is None:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


class TelemetryPusher(threading.Thread):
    """Reads arm_status.json at TELEM_HZ and pushes batches to robot.pushTelemetry."""

    def __init__(self, convex: ConvexClient, job_id: str):
        super().__init__(daemon=True)
        self.convex = convex
        self.job_id = job_id
        self._running = True
        self._err: Exception | None = None

    def run(self):
        batch: list[dict] = []
        period = 1.0 / TELEM_HZ
        try:
            while self._running:
                t0 = time.time()
                st = arm_io.read_status()
                if st and "state" in st:
                    state = st["state"]
                    try:
                        joints = [float(state[f"J{m}"]) for m in range(1, 7)]
                    except (KeyError, TypeError):
                        joints = None
                    if joints is not None:
                        gt = (st.get("gripper") or {}).get("torque")
                        pour_deg = state.get("J6")
                        batch.append({
                            "t": time.time() * 1000.0,
                            "joints": joints,
                            "gripperTorque": None if gt is None else float(gt),
                            "pourDeg": None if pour_deg is None else float(pour_deg),
                        })
                # flush every ~1 s to avoid tiny mutations
                if len(batch) >= TELEM_HZ or (batch and time.time() - batch[0]["t"] / 1000.0 > 1.5):
                    try:
                        self.convex.mutation("robot:pushTelemetry", {
                            "jobId": self.job_id, "samples": batch,
                        })
                    except Exception as e:
                        self._err = e
                    batch = []
                time.sleep(max(0.0, period - (time.time() - t0)))
            if batch:
                try:
                    self.convex.mutation("robot:pushTelemetry", {
                        "jobId": self.job_id, "samples": batch,
                    })
                except Exception as e:
                    self._err = e
        except Exception as e:
            self._err = e

    def stop(self, timeout: float = 2.0):
        self._running = False
        self.join(timeout=timeout)
        if self._err:
            # non-fatal; a push failure shouldn't abort the pour, we just log it
            print(f"[telem] push error: {self._err!r}")


def _step_command(step: dict, dry_run: bool, level: str,
                  convex_url: str | None = None, job_id: str | None = None) -> list[str]:
    """Translate a plan step into the exact subprocess argv for run_task-style execution."""
    name = step["name"]
    a = step.get("args") or {}
    dry = ["--dry-run"] if dry_run else []
    if name == "grasp":
        return ["primitives/grasp.py", str(int(a["vial"]))] + dry
    if name == "lift":
        return ["primitives/lift.py"] + dry
    if name == "traverse":
        return ["primitives/traverse.py", str(int(a["channel"]))] + dry
    if name == "lower":
        return ["primitives/lower.py"] + dry
    if name == "adaptive_pour":
        extra = []
        if convex_url and job_id:
            extra = ["--convex-url", convex_url, "--job-id", job_id]
        return ["adaptive_pour.py", f"{float(a['targetMl'])}",
                "--channel", str(int(a["channel"])),
                "--level", level, "--yes"] + extra + dry
    if name == "untilt":
        return ["primitives/untilt.py"] + dry
    if name == "return_vial":
        return ["primitives/return_vial.py", str(int(a["vial"]))] + dry
    if name == "home":
        return ["primitives/home.py"] + dry
    raise ValueError(f"unknown primitive {name!r}")


def run_job(convex: ConvexClient, job: dict, args: argparse.Namespace) -> None:
    job_id = job["jobId"]
    print(f"\n== running job {job_id} ({job['targetMl']:.2f} mL -> ch{int(job['channel'])}/v{int(job['vial'])}) ==")
    convex.mutation("jobs:setStatus", {"jobId": job_id, "status": "running"})
    convex.mutation("events:log", {
        "kind": "info", "source": "robot", "jobId": job_id,
        "message": "robot client picked up job",
    })

    pusher = TelemetryPusher(convex, job_id)
    pusher.start()

    status_at_exit = "done"
    fail_reason: str | None = None
    pours_log_before = POURS_LOG.stat().st_mtime if POURS_LOG.exists() else 0.0

    try:
        for step in job["plan"]:
            # abort on e-stop before each step
            stop = convex.query("control:stopRequested", {})
            if stop and stop.get("stop"):
                status_at_exit = "estop"
                fail_reason = "e-stop active"
                break

            name = step["name"]
            if name == "adaptive_pour":
                convex.mutation("jobs:setStatus", {"jobId": job_id, "status": "pouring",
                                                   "currentStep": name})
            else:
                convex.mutation("jobs:setStatus", {"jobId": job_id, "status": "running",
                                                   "currentStep": name})

            step_id = convex.mutation("steps:start", {"jobId": job_id, "name": name})
            cmd = _step_command(step, args.dry_run, args.level,
                                convex_url=args.convex_url, job_id=job_id)
            print(f"  [{name}] {' '.join(cmd)}")
            try:
                r = subprocess.run([str(VENV_PY)] + cmd, cwd=str(HERE),
                                   timeout=args.step_timeout)
                ok = r.returncode == 0
                reason = None if ok else f"exit code {r.returncode}"
            except subprocess.TimeoutExpired:
                ok = False
                reason = f"timeout after {args.step_timeout}s"
            except Exception as e:
                ok = False
                reason = f"spawn failed: {e!r}"

            convex.mutation("steps:end", {"stepId": step_id, "ok": ok,
                                          "reason": reason})

            if name == "adaptive_pour" and ok:
                # record the pour from pours.jsonl (newest line that post-dates the start)
                time.sleep(0.2)  # let adaptive_pour flush
                last = read_last_pour()
                if last and POURS_LOG.stat().st_mtime > pours_log_before:
                    convex.mutation("pours:record", {
                        "jobId": job_id,
                        "targetMl": float(last.get("target_ml", job["targetMl"])),
                        "pouredMl": float(last.get("poured_ml", 0.0)),
                        "onsetDeg": float(last.get("onset_deg", 0.0)),
                        "pourDeg": float(last.get("chosen_angle_deg", 0.0)),
                        "measuredMlPerS": float(last.get("measured_rate_ml_per_s", 0.0)),
                    })
                else:
                    print("  [pour] no new pour-log entry to record")

            if not ok:
                status_at_exit = "failed"
                fail_reason = reason
                break
    finally:
        pusher.stop()
        patch = {"jobId": job_id, "status": status_at_exit}
        convex.mutation("jobs:setStatus", patch)
        if fail_reason:
            convex.mutation("events:log", {
                "kind": "error" if status_at_exit != "done" else "info",
                "source": "robot", "jobId": job_id,
                "message": fail_reason,
            })
        print(f"== job {job_id} -> {status_at_exit} {('('+fail_reason+')') if fail_reason else ''} ==")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between nextForRobot polls")
    ap.add_argument("--dry-run", action="store_true", help="dry-run every primitive (no motion)")
    ap.add_argument("--level", default="auto", choices=["auto", "manual", "opencv"])
    ap.add_argument("--step-timeout", type=float, default=120.0)
    ap.add_argument("--keep-server", action="store_true", help="don't tear down arm_server at exit")
    ap.add_argument("--once", action="store_true", help="run one job then exit")
    args = ap.parse_args()

    url = read_convex_url()
    args.convex_url = url  # cached so adaptive_pour subprocesses can push levels back
    print(f"robot_client: convex @ {url}")
    convex = ConvexClient(url)

    print("ensuring arm_server is running ...")
    server_proc = arm_io.ensure_server()
    print(f"arm_server: {'started by robot_client' if server_proc else 'already running'}")

    try:
        while True:
            try:
                job = convex.query("jobs:nextForRobot", {})
            except Exception as e:
                print(f"[poll] query failed: {e!r}; retrying ...")
                time.sleep(args.poll)
                continue
            if job is None:
                time.sleep(args.poll)
                continue
            run_job(convex, job, args)
            if args.once:
                break
    except KeyboardInterrupt:
        print("\nrobot_client: KeyboardInterrupt")
    finally:
        if server_proc and not args.keep_server:
            arm_io.stop_server()
            try:
                server_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server_proc.terminate()


if __name__ == "__main__":
    main()
