"""Full-rate arm trace for trajectory analysis: robotics/logs/arm_trace.jsonl, one line per control tick.

`record(...)` is called from arm_server's 100 Hz loop. It only appends to a list; the lines are
written in one go every FLUSH_S, and any OSError is swallowed, so the trace can never stall or
break the loop. One line: {"t": epoch s, "cmd": [J1..J6 deg], "pos": [deg], "vel": [deg/s],
"tau": [N.m], "hz": loop rate, "op": current op}. software/laminar/tiger.py tails this file while
a run is active and stores it in the arm_trace hypertable. The file rolls over to arm_trace.1.jsonl
at MAX_BYTES. LAMINAR_TRACE=0 turns it off.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent / "logs" / "arm_trace.jsonl"
FLUSH_S = 0.2
MAX_BYTES = 50_000_000
ENABLED = os.environ.get("LAMINAR_TRACE", "1") != "0"

_buf: list[str] = []
_last_flush = 0.0


def record(cmd, pos, vel, tau, hz=None, op=None) -> None:
    global _last_flush
    if not ENABLED:
        return
    _buf.append(json.dumps({"t": round(time.time(), 4), "cmd": [round(x, 3) for x in cmd],
                            "pos": [round(x, 3) for x in pos], "vel": [round(x, 2) for x in vel],
                            "tau": [round(x, 3) for x in tau], "hz": hz, "op": op}, separators=(",", ":")))
    now = time.perf_counter()
    if now - _last_flush >= FLUSH_S:
        _last_flush = now
        flush()


def flush() -> None:
    if not _buf:
        return
    lines = "\n".join(_buf) + "\n"
    _buf.clear()
    try:
        PATH.parent.mkdir(exist_ok=True)
        if PATH.exists() and PATH.stat().st_size > MAX_BYTES:
            os.replace(PATH, PATH.with_name("arm_trace.1.jsonl"))
        with PATH.open("a") as f:
            f.write(lines)
    except OSError:
        pass  # best-effort; never block the control loop
