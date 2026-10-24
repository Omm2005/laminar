"""Pump robotics/joint_state.json -> Convex robot.pushTelemetry at ~10 Hz.

Reads the file written by robotics/joint_state.py every ~100 ms, batches samples,
and sends one mutation per batch so Convex isn't hit for every control step.
Pure one-way, read-only on the robot side; a failed push just skips a batch.

Usage:
    python console/bridge/stream_joints.py [--job-id <id>] [--period 0.1] [--batch 10]

Env:
    CONVEX_URL   deployment URL. If unset, parsed from console/.env.local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from convex import ConvexClient

HERE = Path(__file__).resolve().parent
LAMINAR = HERE.parent.parent
JOINT_STATE_FILE = LAMINAR / "robotics" / "joint_state.json"
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


def read_sample() -> dict | None:
    try:
        return json.loads(JOINT_STATE_FILE.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default=None, help="tag telemetry with a Convex jobs id")
    ap.add_argument("--period", type=float, default=0.1, help="seconds between reads")
    ap.add_argument("--batch", type=int, default=10, help="samples per push")
    a = ap.parse_args()

    url = read_convex_url()
    print(f"stream_joints: {JOINT_STATE_FILE} -> {url} (job={a.job_id or '-'}, "
          f"{int(1/a.period)} Hz, batch {a.batch})")
    convex = ConvexClient(url)

    last_t = 0.0
    batch: list[dict] = []
    try:
        while True:
            s = read_sample()
            if s and s.get("t") and s["t"] != last_t:
                last_t = s["t"]
                sample = {
                    "t": float(s["t"]),
                    "joints": [float(x) for x in s.get("joints", [])],
                }
                if "commanded" in s:
                    sample["commanded"] = [float(x) for x in s["commanded"]]
                batch.append(sample)
            if len(batch) >= a.batch:
                payload: dict = {"samples": batch}
                if a.job_id:  # omit the key when no job is active; v.optional rejects null
                    payload["jobId"] = a.job_id
                try:
                    convex.mutation("robot:pushTelemetry", payload)
                except Exception as e:
                    print(f"[stream_joints] push failed, skipping batch: {e!r}", file=sys.stderr)
                batch = []
            time.sleep(a.period)
    except KeyboardInterrupt:
        if batch:
            payload = {"samples": batch}
            if a.job_id:
                payload["jobId"] = a.job_id
            try:
                convex.mutation("robot:pushTelemetry", payload)
            except Exception:
                pass
        print()
        print("stream_joints: stopped")


if __name__ == "__main__":
    main()
