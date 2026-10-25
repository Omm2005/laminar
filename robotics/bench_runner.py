"""Run the dashboard's approved runs on the REAL arm: python bench_runner.py

The website (laminar-web) plans a run, a named person approves it, and it waits at /bench/next. This takes it, runs
every pour with pour_episode.py (grab, carry, camera pour, back to the slot, both cameras logged), and reports to
/bench/update: running, the step, each measured pour (tube, well, target mL, poured mL), then done or aborted.
The dashboard's Run tab, Run log & QC, Audit and the assistant all show it like any other run.

    terminal 1:  python mac_run.py arm_server.py --hold-grip     (or python arm_server.py on Windows)
    terminal 2:  python publish.py                              (cameras + joints to the website, optional)
    terminal 3:  python bench_runner.py                         (this: waits for approved runs)

Before each run it prints the pours and waits for Enter here (s = skip it), so the person at the arm starts it.
--yes starts approved runs without asking (only with someone at the e-stop).

Pours: tube B, 1 mL into well 2 -> job "B2b:+N" (add N % of a full column; full = 95 % of the 4.5 mL box, see
pour_episode.TARGET_CAP / CAPACITY_ML). A tube poured into several wells is grabbed once per well. The poured mL
is what the side camera measured in that container: its level at the end of the job minus at the start.

Settings: BENCH_TOKEN (environment or the repo's .env, as publish.py), LAMINAR_SITE or --site.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from publish import SITE, env_token  # noqa: E402

CAPACITY_ML = 4.5      # mL in the level box (pour_episode.CAPACITY_ML)
TARGET_CAP = 0.95      # "full" = this fraction of the box (pour_episode.TARGET_CAP)
FULL_ML = CAPACITY_ML * TARGET_CAP


class Site:
    def __init__(self, site: str, token: str):
        self.site, self.token = site.rstrip("/"), token

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.site + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")

    def next(self):
        return self._call("GET", "/bench/next")

    def update(self, run_id: str, **fields) -> None:
        try:
            self._call("POST", "/bench/update", {"id": run_id, **fields})
        except (urllib.error.URLError, OSError) as e:          # never stop the arm over a lost update
            print(f"  (could not report to the website: {e})")


def jobs_for(run: dict) -> list[dict]:
    """The run's pours as pour_episode jobs, in order."""
    out = []
    for task in run.get("tasks") or []:
        for p in task["pours"]:
            ml, well = float(p["ml"]), int(p["well"])
            if not 1 <= well <= 4:
                raise ValueError(f"well {well}: the tray has containers a-d (wells 1-4)")
            pct = 100.0 * ml / FULL_ML
            if pct > 100.0:
                raise ValueError(f"{ml:g} mL is more than a full container ({FULL_ML:.2f} mL)")
            job = f"{task['tube']}2{'abcd'[well - 1]}"
            out.append({"tube": task["tube"], "well": well, "ml": ml, "job": job, "spec": f"{job}:+{pct:.1f}",
                        "column": "ABCD"[well - 1]})
    return out


def pour_steps(moves: list[str]) -> list[int]:
    """Index of each pour(...) move in the run's move list (the dashboard's progress bar)."""
    return [i for i, m in enumerate(moves or []) if m.startswith("pour(")]


def run_one(site: Site, run: dict, a) -> None:
    rid = run["id"]
    try:
        jobs = jobs_for(run)
    except ValueError as e:
        print(f"  refused: {e}")
        site.update(rid, status="aborted", reason=f"bench refused the plan: {e}")
        return
    if not jobs:
        site.update(rid, status="aborted", reason="the plan has no pours")
        return
    print(f"\n=== approved run {rid[-6:]}: " + ", ".join(f"tube {j['tube']} {j['ml']:g} mL into container "
                                                          f"{'abcd'[j['well'] - 1]} (well {j['well']})" for j in jobs))
    if not a.yes:
        ans = input("  Start it on the arm? Enter = start, s = skip: ").strip().lower()
        if ans.startswith("s"):
            site.update(rid, status="aborted", reason="skipped at the bench")
            print("  skipped")
            return
    steps = pour_steps(run.get("moves") or [])
    site.update(rid, status="running", step=0)
    cmd = [sys.executable, "-u", str(HERE / "pour_episode.py"), *[j["spec"] for j in jobs], "--run",
           "--side-cam", str(a.side_cam), "--wrist-cam", str(a.wrist_cam), *a.episode_args]
    print("  " + " ".join(cmd[1:]))
    out_dir, k = None, 0
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            print("  | " + line.rstrip())
            m = re.match(r"logging to (\S+)", line.strip())
            if m:
                out_dir = HERE / m.group(1)
            if line.startswith("=== "):                       # pour_episode starts the next job
                if k < len(steps):
                    site.update(rid, step=steps[k])
                k += 1
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.wait()
        rc, stopped = -1, True
    else:
        stopped = False
    summary = {}
    if out_dir and (out_dir / "summary.json").exists():
        summary = json.loads((out_dir / "summary.json").read_text())
    for rec, job in zip(summary.get("jobs") or [], jobs):
        try:
            poured = rec["end"][job["column"]]["ml"] - rec["start"][job["column"]]["ml"]
        except (KeyError, TypeError):
            continue                                         # the job never got to the end: no measurement
        site.update(rid, pour={"tube": job["tube"], "well": job["well"], "targetMl": job["ml"],
                               "pouredMl": round(max(0.0, poured), 3)})
        print(f"  measured: tube {job['tube']} -> container {'abcd'[job['well'] - 1]}: {poured:.2f} mL "
              f"(target {job['ml']:g})")
    err = summary.get("error")
    if stopped:
        site.update(rid, status="aborted", reason="stopped at the bench (Ctrl+C)")
    elif rc != 0 or err:
        site.update(rid, status="aborted", reason=err or f"pour_episode.py exited with code {rc}")
    else:
        site.update(rid, status="done")
    print(f"  run {rid[-6:]}: {'STOPPED' if stopped or rc or err else 'done'}"
          + (f" ({err})" if err else "") + (f"; logs in {out_dir.relative_to(HERE)}" if out_dir else ""))
    if stopped:
        raise KeyboardInterrupt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=SITE, help="the website's backend (https://<name>.convex.site)")
    ap.add_argument("--yes", action="store_true", help="start approved runs without asking here")
    ap.add_argument("--side-cam", type=int, default=2)
    ap.add_argument("--wrist-cam", type=int, default=1)
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between checks for an approved run")
    ap.add_argument("episode_args", nargs="*", help="passed on to pour_episode.py (after --), e.g. -- --pour-mode roll")
    a = ap.parse_args()
    token = env_token(HERE)
    if not token:
        print("no BENCH_TOKEN: put BENCH_TOKEN=... in the repo's .env (same as publish.py)")
        return 1
    site = Site(a.site, token)
    print(f"bench runner: waiting for approved runs from {a.site} (Ctrl+C stops)")
    down = False
    while True:
        try:
            run = site.next()
            down = False
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("the website refused the BENCH_TOKEN (401): check it matches the Convex env variable")
                return 1
            run = None
        except (urllib.error.URLError, OSError) as e:
            if not down:
                print(f"website unreachable ({e}); retrying")
            down, run = True, None
        if run:
            try:
                run_one(site, run, a)
            except KeyboardInterrupt:
                print("\nstopped")
                return 0
            print("\nwaiting for the next approved run")
        else:
            try:
                time.sleep(a.poll)
            except KeyboardInterrupt:
                print("\nstopped")
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
