"""Console bridge helper: non-blocking 10 Hz writer of robotics/joint_state.json.

`write(joints)` is safe to call from any 100 Hz control loop; it skips if less than
~100 ms since the last write and swallows any OSError, so a locked file never
interrupts the loop. Nothing in robotics/ reads this file — it's consumed by the
console/bridge/stream_joints.py pump that forwards samples to Convex.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "joint_state.json"
_MIN_PERIOD_S = 0.1  # 10 Hz
_last_t = 0.0


def write(joints) -> None:
    """Dump {"t": ms, "joints": [j1..j6]} if at least 100 ms since the last write."""
    global _last_t
    now = time.perf_counter()
    if now - _last_t < _MIN_PERIOD_S:
        return
    _last_t = now
    tmp = _PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(
            {"t": time.time() * 1000.0, "joints": [float(x) for x in joints]},
            separators=(",", ":"),
        ))
        os.replace(tmp, _PATH)
    except OSError:
        pass  # best-effort; never block the control loop
