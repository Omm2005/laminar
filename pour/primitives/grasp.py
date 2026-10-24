"""Grasp vial N: expects current pose near vial_N_grasp, opens the jaws, then closes until contact.

Usage:  python pour/primitives/grasp.py N [--dry-run] [--speed 5] [--close-torque 1.2] [--hold-torque 1.0]
"""
import argparse

from _common import pose_joints, pose_gripper, require_near
import arm_io


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("N", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--speed", type=float, default=5.0)
    ap.add_argument("--close-torque", type=float, default=1.2)
    ap.add_argument("--hold-torque", type=float, default=1.0)
    args = ap.parse_args()

    pose = f"vial_{args.N}_grasp"
    expected = pose_joints(pose)
    g_open = pose_gripper(pose) or 60.0

    current = arm_io.current_joints()
    require_near(expected, current)

    print(f"grasp vial {args.N}: open gripper -> {g_open:.1f}, grasp speed={args.speed} "
          f"close_torque={args.close_torque} hold_torque={args.hold_torque}")
    if args.dry_run:
        return

    arm_io.send_cmd("gripper", target=g_open, speed=30.0)
    st = arm_io.send_cmd("grasp", speed=args.speed,
                         close_torque=args.close_torque, hold_torque=args.hold_torque)
    print(st.get("result"))
    g = st.get("grasp") or {}
    if not g.get("contact", False):
        raise SystemExit(f"grasp failed: {g}")


if __name__ == "__main__":
    main()
