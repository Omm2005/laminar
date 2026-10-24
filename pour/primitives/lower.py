"""Lower the tool straight down by --mm (orientation held, 4 waypoints).

Usage:  python pour/primitives/lower.py [--mm 50] [--speed 8] [--dry-run]
"""
import argparse

from _common import waypoints_xyz
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mm", type=float, default=50.0)
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    start = arm_io.current_joints()
    waypoints_xyz(start, 0.0, 0.0, -args.mm / 1000.0, steps=4, speed=args.speed,
                  dry_run=args.dry_run, label=f"lower -{args.mm:.0f}mm")


if __name__ == "__main__":
    main()
