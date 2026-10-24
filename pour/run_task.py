"""Parse a one-line natural-language task, print the plan, run the primitives in order.

Supported forms (case insensitive):
  pour <N> ml into channel <M> [from vial <V>]
  pour <half|quarter|third|all> of channel <M> [from vial <V>]

Vial defaults to 1 when omitted. The target volume for the fraction form is
channels.yaml[M].capacity_ml * fraction.

Flags:
  --confirm-each   ask before every primitive
  --level          manual | opencv  (passed to adaptive_pour)
  --dry-run        every primitive prints its plan but nothing moves
  --keep-server    don't tear down arm_server at the end even if we started it

A file named STOP in teleop/ aborts before the next step (does NOT interrupt an in-flight move).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import arm_io

LAMINAR = HERE.parent
TELEOP = LAMINAR / "teleop"
VENV_PY = LAMINAR / ".venv" / "Scripts" / "python.exe"
STOP_FILE = TELEOP / "STOP"

CHANNELS = (yaml.safe_load((HERE / "data" / "channels.yaml").read_text()) or {}).get("channels", {})
FRACTIONS = {"half": 0.5, "quarter": 0.25, "third": 1.0 / 3.0, "all": 1.0}

NUM_ML_RE = re.compile(r"pour\s+([\d.]+)\s*ml?\s+into\s+channel\s+(\d+)(?:\s+from\s+vial\s+(\d+))?", re.I)
FRAC_RE   = re.compile(r"pour\s+(\w+)\s+of\s+channel\s+(\d+)(?:\s+from\s+vial\s+(\d+))?", re.I)


def parse(task):
    """Returns (target_ml, channel, vial). Raises SystemExit on parse failure."""
    m = NUM_ML_RE.match(task.strip())
    if m:
        return float(m[1]), int(m[2]), int(m[3] or 1)
    m = FRAC_RE.match(task.strip())
    if m:
        word = m[1].lower()
        if word not in FRACTIONS:
            raise SystemExit(f"unknown fraction {word!r} (use: {sorted(FRACTIONS)})")
        channel = int(m[2])
        if channel not in CHANNELS:
            raise SystemExit(f"channel {channel} not in channels.yaml")
        cap = float(CHANNELS[channel]["capacity_ml"])
        return cap * FRACTIONS[word], channel, int(m[3] or 1)
    raise SystemExit(f"could not parse task: {task!r}")


def run_step(cmd, confirm, dry_run_label):
    if STOP_FILE.exists():
        raise SystemExit(f"STOP file present at {STOP_FILE} — aborting before next step")
    pretty = " ".join(cmd)
    print(f"\n>>> {pretty}" + (f"   [{dry_run_label}]" if dry_run_label else ""))
    if confirm:
        if input("run this step? [y/N] ").strip().lower() != "y":
            raise SystemExit("aborted at --confirm-each prompt")
    r = subprocess.run([str(VENV_PY)] + cmd, cwd=str(HERE))
    if r.returncode != 0:
        raise SystemExit(f"step exited with code {r.returncode}: {pretty}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help='e.g. "pour half of channel 3 from vial 1"')
    ap.add_argument("--confirm-each", action="store_true")
    ap.add_argument("--level", default="manual", choices=["manual", "opencv"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-server", action="store_true")
    args = ap.parse_args()

    target_ml, channel, vial = parse(args.task)
    print(f"task     : {args.task.strip()!r}")
    print(f"parsed   : target={target_ml:.2f} mL  channel={channel}  vial={vial}")
    print(f"level    : {args.level}")
    STOP_FILE.unlink(missing_ok=True)

    # arm_server is needed even for --dry-run so each primitive can read the current pose for its FK plan.
    # Starting arm_server enables motors to hold their current position — it does not move the arm.
    print("ensuring arm_server is running ...")
    proc = arm_io.ensure_server()
    print(f"arm_server: {'started by run_task' if proc else 'already running'}")

    dry = ["--dry-run"] if args.dry_run else []
    label = "DRY-RUN" if args.dry_run else ""
    steps = [
        ["primitives/grasp.py", str(vial)] + dry,
        ["primitives/lift.py"] + dry,
        ["primitives/traverse.py", str(channel)] + dry,
        ["primitives/lower.py"] + dry,
        ["adaptive_pour.py", str(target_ml), "--channel", str(channel), "--level", args.level, "--yes"] + dry,
        ["primitives/untilt.py"] + dry,
        ["primitives/lift.py"] + dry,
        ["primitives/return_vial.py", str(vial)] + dry,
        ["primitives/home.py"] + dry,
    ]
    print(f"\nplan ({len(steps)} steps):")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {' '.join(s)}")

    try:
        for s in steps:
            run_step(s, confirm=args.confirm_each, dry_run_label=label)
        print("\nTASK COMPLETE")
    finally:
        if proc and not args.keep_server:
            print("tearing down arm_server (we started it) ...")
            arm_io.stop_server()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.terminate()


if __name__ == "__main__":
    main()
