"""Command-line tool for the shared moves. Plans by default; only moves the arm with --run.

Run from the robotics/ folder, with arm_server.py already running:

    python move.py lift 100                     # plan a 100 mm lift from the current pose, print it
    python move.py lift 100 --run               # ... and do it
    python move.py lower 60 --run
    python move.py travel well_2 --run          # slide flat to above well_2 (taught in spots.yaml)
    python move.py travel well_2 --lower-next 60   # also warn if lowering 60 mm afterwards misses the taught height
    python move.py teach well_2                 # save the arm's current pose as well_2
    python move.py from-recording slot_A recordings/<run>/data.csv   # last pose of a grab = slot_A

Options
    --run            actually move the arm (otherwise: plan and print only)
    --mode moves|path  how to send the plan to arm_server (see arm_client.py); default moves
    --speed MM_S     override the peak gripper speed
    --no-workspace   skip the workspace box check from bench.yaml (only for testing poses near its edges)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path as FilePath

import yaml

import motion as M
from arm_client import ArmClient
from spots import get_spot, save_spot, spot_from_recording

BENCH = FilePath(__file__).resolve().parent / "bench.yaml"


def workspace_box():
    """((xmin, ymin, zmin), (xmax, ymax, zmax)) in mm from bench.yaml, or None if not set."""
    try:
        ws = (yaml.safe_load(BENCH.read_text()) or {}).get("workspace_mm")
        return (ws["min"], ws["max"]) if ws else None
    except OSError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["lift", "lower", "travel", "teach", "from-recording"])
    ap.add_argument("arg", help="mm for lift/lower, spot name for travel/teach/from-recording")
    ap.add_argument("csv", nargs="?", help="data.csv path, for from-recording")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--mode", choices=["moves", "path"], default="moves")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--lower-next", type=float, default=0.0)
    ap.add_argument("--no-workspace", action="store_true")
    a = ap.parse_args()

    # Spot bookkeeping: no motion involved.
    if a.op == "from-recording":
        if not a.csv:
            sys.exit("from-recording needs a data.csv path")
        q = spot_from_recording(a.csv)
        save_spot(a.arg, q)
        print(f"saved {a.arg} = {q}  (gripper at {M.fk_mm(q)[0].round(1)} mm)")
        return

    client = ArmClient(dry_run=not a.run)
    try:
        q_now = client.current_joints()
    except RuntimeError as e:
        sys.exit(str(e))

    if a.op == "teach":
        save_spot(a.arg, q_now)
        print(f"saved {a.arg} = {q_now}  (gripper at {M.fk_mm(q_now)[0].round(1)} mm)")
        return

    ws = None if a.no_workspace else workspace_box()
    kw = {"workspace_mm": ws}
    if a.speed:
        kw["speed_mm_s"] = a.speed
    try:
        if a.op == "lift":
            path = M.plan_lift(q_now, float(a.arg), **kw)
        elif a.op == "lower":
            path = M.plan_lower(q_now, float(a.arg), **kw)
        else:
            path = M.plan_travel(q_now, get_spot(a.arg), label=f"travel to {a.arg}",
                                 height_offset_mm=a.lower_next, **kw)
    except (M.PlanError, KeyError, ValueError) as e:
        sys.exit(f"NOT MOVING: {e}")

    if not a.run:
        print(path.summary())
        print("Plan only. Add --run to move the arm.")
        return
    try:
        status = client.run(path, mode=a.mode)
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"STOPPED: {e}")
    print(status.get("result"))


if __name__ == "__main__":
    main()
