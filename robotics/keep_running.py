"""Bring up the bench when the dashboard has work: python keep_running.py

Every few seconds it asks the website whether an approved run is waiting (GET /bench/next: a read, it does not take
the run) and whether a run is going on here (pour_episode.py running). While there is work, it makes sure
arm_server.py, publish.py and bench_runner.py are all running and starts any that is missing (arm_server first,
each in its own console window). With no work it starts nothing: scripts that stop (arm_server exits after 15 min
idle, a `rest`, or a safety stop) stay stopped until the next approved run. Copies already running are adopted,
never restarted. A script that dies within a minute of starting is retried after 5, 15, 30, 60, 120 s.

To stop it for good: create robotics/KEEP_RUNNING_OFF (nothing is started while it exists), or close this window.
Log: robotics/logs/keep_running.log. Needs BENCH_TOKEN like publish.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_runner import Site  # noqa: E402
from publish import SITE, env_token  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPTS = ["arm_server.py", "publish.py", "bench_runner.py"]
OFF = HERE / "KEEP_RUNNING_OFF"
LOG = HERE / "logs" / "keep_running.log"
CHECK_S = 3.0
FAST_EXIT_S = 60.0        # a script that dies this soon after starting counts as failing...
BACKOFF_S = [0, 5, 15, 30, 60, 120]   # ...and waits longer each time before the next start


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except OSError:
        pass


def running(script: str) -> list[int]:
    """PIDs of python processes running `script` (any folder, any arguments)."""
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = p.info["cmdline"] or []
            if p.info["pid"] != psutil.Process().pid and "python" in (p.info["name"] or "").lower() \
                    and any(Path(c).name == script for c in cmd[1:]):
                out.append(p.info["pid"])
        except (psutil.Error, OSError):
            continue
    return out


def start(script: str) -> int:
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    p = subprocess.Popen([sys.executable, "-u", str(HERE / script)], cwd=HERE, creationflags=flags)
    return p.pid


def work_waiting(site) -> bool:
    """An approved run is waiting on the website, or a run is going on here."""
    if running("pour_episode.py"):
        return True
    try:
        return site.next() is not None
    except Exception:                    # website unreachable: start nothing new, leave what runs alone
        return False


def main() -> int:
    token = env_token(HERE)
    if not token:
        print("no BENCH_TOKEN: put BENCH_TOKEN=... in the repo's .env (same as publish.py)")
        return 1
    site = Site(SITE, token)
    state = {s: {"started": 0.0, "fails": 0, "next": None} for s in SCRIPTS}
    log(f"keep_running: starts {', '.join(SCRIPTS)} when an approved run is waiting (create {OFF.name} to pause)")
    for s in SCRIPTS:
        pids = running(s)
        if pids:
            log(f"  {s}: already running (pid {', '.join(map(str, pids))}), adopted")
    paused = False
    while True:
        if OFF.exists():
            if not paused:
                log(f"{OFF.name} exists: not restarting anything")
            paused = True
            time.sleep(CHECK_S)
            continue
        if paused:
            log(f"{OFF.name} removed: watching again")
            paused = False
        if not work_waiting(site):
            for st in state.values():
                st["next"], st["started"], st["fails"] = None, 0.0, 0     # next time, start right away
            time.sleep(CHECK_S)
            continue
        now = time.time()
        for s in SCRIPTS:
            st = state[s]
            if running(s):
                continue
            if st["next"] is None:                              # just found it gone: decide when to start it
                quick = bool(st["started"]) and now - st["started"] < FAST_EXIT_S
                st["fails"] = st["fails"] + 1 if quick else 0
                wait = BACKOFF_S[min(st["fails"], len(BACKOFF_S) - 1)]
                st["next"] = now + wait
                log(f"{s} is not running" + (f" (stopped {st['fails']}x within {FAST_EXIT_S:.0f} s of starting)"
                                             if quick else "") + (f": starting it in {wait} s" if wait else ""))
            if now < st["next"]:
                continue
            if s == "arm_server.py":
                (HERE / "arm_cmd.json").unlink(missing_ok=True)    # never replay a stale command
            pid = start(s)
            st["started"], st["next"] = time.time(), None
            log(f"started {s} (pid {pid})")
            if s == "arm_server.py":
                time.sleep(5)                                    # let it open the CAN adapter alone first
        time.sleep(CHECK_S)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("keep_running: stopped (the scripts keep running)")
