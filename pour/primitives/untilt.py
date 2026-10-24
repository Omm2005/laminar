"""Rotate pour_joint back to rest_deg (held).  All other joints held at their current commands.

Usage:  python pour/primitives/untilt.py [--speed <tilt_speed_deg_s from pour.yaml>] [--dry-run]
"""
import argparse

from _common import POUR, send_move
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=float(POUR.get("tilt_speed_deg_s", 10.0)))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pour_joint = int(POUR["pour_joint"])
    rest_deg = float(POUR["rest_deg"])
    target = dict(arm_io.current_joints())
    target[pour_joint] = rest_deg
    send_move(target, speed=args.speed, dry_run=args.dry_run,
              label=f"untilt J{pour_joint}->{rest_deg:.1f}")


if __name__ == "__main__":
    main()
