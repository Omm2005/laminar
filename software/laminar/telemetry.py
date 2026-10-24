"""Telemetry: every event goes to a local JSONL file, always.

log(event, data) is safe to call from any thread. Forwarders (Tiger Data, dashboard)
are added later with add_forwarder() and never block or break local logging.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_path = Path(os.environ.get("LAMINAR_LOG", REPO_ROOT / "logs" / "telemetry.jsonl"))
_run_id: str | None = None
_forwarders: list[Callable[[dict], None]] = []


def configure(path: str | Path | None = None, run_id: str | None = None) -> None:
    global _path, _run_id
    if path is not None:
        _path = Path(path)
    _run_id = run_id


def path() -> Path:
    return _path


def add_forwarder(fn: Callable[[dict], None]) -> None:
    _forwarders.append(fn)


def _jsonable(x):
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, (set, tuple)):
        return list(x)
    return str(x)


def log(event: str, data: dict | None = None) -> None:
    rec = {"ts": time.time(), "event": event, "run_id": _run_id, **(data or {})}
    line = json.dumps(rec, default=_jsonable)
    with _lock:
        _path.parent.mkdir(parents=True, exist_ok=True)
        with _path.open("a") as f:
            f.write(line + "\n")
    for fwd in _forwarders:
        try:
            fwd(rec)
        except Exception:
            pass  # forwarding is best effort; the JSONL is the source of truth


def read(p: str | Path | None = None) -> list[dict]:
    p = Path(p) if p is not None else _path
    if not p.exists():
        return []
    with p.open() as f:
        return [json.loads(line) for line in f if line.strip()]
