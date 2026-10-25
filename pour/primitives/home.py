"""Go to the home pose from poses.yaml.  Default speed 15 (empty arm).

Usage:  python pour/primitives/home.py [--speed 15] [--dry-run]
"""
import argparse

from _common import pose_joints, pose_gripper, send_move
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=15.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target = pose_joints("home")
    g_target = pose_gripper("home")
    send_move(target, speed=args.speed, dry_run=args.dry_run, label="home")
    if not args.dry_run and g_target is not None:
        arm_io.send_cmd("gripper", target=g_target, speed=30.0)


if __name__ == "__main__":
    main()
