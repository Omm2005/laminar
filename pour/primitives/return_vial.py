"""Return vial N: joint-space move to vial_N_above (no-dip check), then release the gripper.

Usage:  python pour/primitives/return_vial.py N [--speed 8] [--dry-run] [--no-release]
"""
import argparse

from _common import pose_joints, pose_gripper, check_no_dip, send_move
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("N", type=int)
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-release", action="store_true", help="don't open the gripper after")
    args = ap.parse_args()

    target_name = f"vial_{args.N}_above"
    target = pose_joints(target_name)
    g_target = pose_gripper(target_name)
    start = arm_io.current_joints()
    check_no_dip(start, target)
    send_move(target, speed=args.speed, dry_run=args.dry_run, label=f"return->{target_name}")
    if args.dry_run:
        if not args.no_release and g_target is not None:
            print(f"return: would release gripper -> {g_target:.1f}")
        return
    if not args.no_release and g_target is not None:
        arm_io.send_cmd("gripper", target=g_target, speed=30.0)


if __name__ == "__main__":
    main()
