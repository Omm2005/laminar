"""Run a plan.json through the executor on the fakes.

    python -m laminar.run plans/aliquot_2ml_blue.json --approve Om
    python -m laminar.run plans/aliquot_2ml_blue.json --approve Om --fault wrong_tube
    python -m laminar.run plans/aliquot_2ml_blue.json            # not approved -> rejected
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .executor import RUNS_DIR, Executor
from .hardware import make_system
from .skills import FAULTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", type=Path)
    ap.add_argument("--approve", metavar="NAME", help="who approved the plan; without it the run is rejected")
    ap.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS))
    ap.add_argument("--speed", type=float, default=0.0, help="time scale: 0 = instant, 1 = real time")
    a = ap.parse_args()

    plan = json.loads(a.plan.read_text())
    report = Executor(make_system(fake=True, time_scale=a.speed, faults=a.fault)).run(plan, approved_by=a.approve)
    print(report.summary())
    print(f"report: {RUNS_DIR / (report.run_id + '.json')}")
    return 0 if report.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
