"""Run one aliquot job on the fakes, step by step, with no executor yet.

    python -m laminar.demo                      # clean run, 2 mL of blue
    python -m laminar.demo --fault wrong_tube   # staged failure
    python -m laminar.demo --liquid red --vial V2 --ml 1.5 --tol 0.4
"""
from __future__ import annotations

import argparse
import uuid

from . import telemetry
from .hardware import make_system
from .skills import FAULTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vial", default="V1")
    ap.add_argument("--liquid", default="blue")
    ap.add_argument("--ml", type=float, default=2.0)
    ap.add_argument("--tol", type=float, default=0.2)
    ap.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS))
    ap.add_argument("--speed", type=float, default=0.0, help="time scale: 0 = instant, 1 = real time")
    a = ap.parse_args()

    run_id = uuid.uuid4().hex[:8]
    telemetry.configure(run_id=run_id)
    s = make_system(fake=True, time_scale=a.speed, faults=a.fault)
    loc = s.skills.locate(a.vial)
    if loc is None:
        print(f"vial {a.vial} is not in the rack")
        return 1
    target = s.bench["station"]["target_id"]

    steps = [
        ("pick", lambda: s.skills.pick(a.vial)),
        ("carry", lambda: s.skills.carry("station")),
        ("pour", lambda: s.skills.pour(target, a.ml, a.liquid)),
        ("verify", lambda: s.skills.verify(target, a.ml, a.tol)),
        ("carry", lambda: s.skills.carry("rack")),
        ("place", lambda: s.skills.place(a.vial, loc.slot)),
    ]
    print(f"run {run_id}: {a.ml} mL of {a.liquid} from {a.vial} into {target}"
          + (f"  [faults: {', '.join(a.fault)}]" if a.fault else ""))
    for name, step in steps:
        r = step()
        info = {k: v for k, v in r.telemetry.items() if k != "duration_s"}
        print(f"  {'OK  ' if r.ok else 'FAIL'} {name:<7} {r.reason}  {info if info else ''}".rstrip())
        if not r.ok:
            print(f"stopped at {name}. log: {telemetry.path()}")
            return 1
    print(f"done. log: {telemetry.path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
