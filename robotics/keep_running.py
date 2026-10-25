"""Keep the bench up: arm_server.py, publish.py and bench_runner.py always running. python keep_running.py

Every few seconds it checks that each script has a running python process (started from anywhere: copies already
running are adopted, never restarted) and starts any that is missing, from this folder, in its own console window
(so you can watch it and Ctrl+C it; Ctrl+C there only restarts it, see below).

    arm_server.py    exits by design: idle for 15 min, a `rest` command, or a safety stop (lost feedback / fault /
                     overheating, after easing to rest). It is started again; a safety stop that repeats quickly
                     backs off (it may be the CAN adapter, which then needs a replug).
    publish.py       cameras + joints to the dashboard.
    bench_runner.py  takes the dashboard's approved runs (they start on their own).

To stop everything for good: create robotics/KEEP_RUNNING_OFF (nothing is restarted while it exists), then close the
windows; or close this window first. Logs: robotics/logs/keep_running.log.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

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


def main() -> int:
    state = {s: {"started": 0.0, "fails": 0, "next": None} for s in SCRIPTS}
    log(f"keep_running: watching {', '.join(SCRIPTS)} (create {OFF.name} to pause restarts)")
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
