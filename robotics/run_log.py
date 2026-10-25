"""Every process that moves the arm is a Tiger Data run, automatically (software/laminar/tiger.py RunLogger).

arm_client.ArmClient calls started() before its first command and failed() when one fails, so grab.py,
transfer.py, episode.py, move.py, run_job.py and anything else built on ArmClient log themselves: an
arm_runs row named after the command line, the 100 Hz arm trace while it runs, and done / failed / aborted
when the process exits. Off (silently) without TIGER_DATABASE_URL, which is read from the environment or
from the repo's .env (gitignored). Nested runs log once: the first one sets LAMINAR_RUN_ID for its children,
and `tiger run` / run_job set it too. LAMINAR_TIGER=0 turns it off.
"""
from __future__ import annotations

import atexit
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENV_DSN = "TIGER_DATABASE_URL"

_run = None          # RunLogger, False when logging is off for this process, None before the first command
_failed: str | None = None
_aborted = False


def _load_env_file() -> None:
    env = REPO / ".env"
    if os.environ.get(ENV_DSN) or not env.exists():
        return
    for line in env.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == ENV_DSN:
            os.environ[ENV_DSN] = v.strip().strip('"').strip("'")


def started() -> None:
    """Start this process's run, once. Never raises: a logging problem must not stop the arm."""
    global _run
    if _run is not None:
        return
    _run = False
    try:
        _load_env_file()
        if os.environ.get("LAMINAR_TIGER") == "0" or not os.environ.get(ENV_DSN) or os.environ.get("LAMINAR_RUN_ID"):
            return
        sys.path.insert(0, str(REPO / "software"))
        from laminar.tiger import RunLogger
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        name = " ".join([Path(sys.argv[0]).name, *sys.argv[1:]])[:120]
        _run = RunLogger.start(run_id, name=name, meta={"argv": sys.argv, "cwd": os.getcwd(), "source": "arm_client"})
        if _run:
            os.environ["LAMINAR_RUN_ID"] = run_id
            print(f"[tiger] logging this run as {run_id}", flush=True)
            prev = sys.excepthook

            def hook(tp, value, tb):
                global _aborted
                _aborted = _aborted or issubclass(tp, KeyboardInterrupt)
                failed(repr(value))
                prev(tp, value, tb)
            sys.excepthook = hook
            atexit.register(_finish)
    except Exception as e:  # missing psycopg, bad URL, ...: run without logging
        print(f"[tiger] not logging this run: {e!r}", flush=True)
        _run = False


def failed(reason: str) -> None:
    global _failed
    _failed = _failed or reason


def _finish() -> None:
    if not _run:
        return
    status = "aborted" if _aborted else ("failed" if _failed else "done")
    try:
        _run.finish(status, {"error": _failed} if _failed else None)
    except Exception:
        pass
