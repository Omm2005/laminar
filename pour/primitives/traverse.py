"""Traverse horizontally to channel_N_above (joint-space move with a no-dip safety check).

Usage:  python pour/primitives/traverse.py N [--speed 8] [--dry-run]
"""
import argparse

from _common import pose_joints, check_no_dip, send_move
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("N", type=int, help="channel number 1..12")
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target_name = f"channel_{args.N}_above"
    target = pose_joints(target_name)
    start = arm_io.current_joints()
    check_no_dip(start, target)
    send_move(target, speed=args.speed, dry_run=args.dry_run,
              label=f"traverse->{target_name}")


if __name__ == "__main__":
    main()
