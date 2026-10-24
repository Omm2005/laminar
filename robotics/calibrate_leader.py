"""Calibrate the FashionStar reBot Arm 102 leader: sets every servo's origin (stored in the servo) at the zero pose.

Zero pose (same as the follower's resting pose): arm folded down, all joints at their reference marks,
gripper fully closed. Reference photo: https://files.seeedstudio.com/wiki/robotics/projects/lerobot/102_zeroposition.jpg

Usage:  python calibrate_leader.py [--yes]
"""
import argparse
import json
import time

from leader_arm import LEADER_CAL_FILE, MAPPING, LeaderArm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="don't wait for ENTER (arm is already in the zero pose)")
    args = ap.parse_args()

    leader = LeaderArm()
    try:
        before = leader.read_raw()
        print("Before:", "  ".join(f"id{i}={a:7.1f}" for i, a in before.items()))
        if not args.yes:
            input("Put the leader in its zero pose with the gripper closed, then press ENTER...")

        for sid in MAPPING:
            leader.bus.unlock(sid)
            time.sleep(0.01)
            leader.bus.set_origin_point(sid)
            time.sleep(0.01)
            leader.bus.reset_multi_turn(sid)
        time.sleep(0.5)

        # The library flags an exact 0.0 reading as unreliable and returns the previous (pre-calibration)
        # angle instead, so judge only on fresh readings: none of them may be more than 2 deg from zero.
        off = {}
        for _ in range(50):
            for sid, m in leader.bus.sync_monitor(list(MAPPING)).items():
                if m is not None and m.reliable and abs(m.angle_deg) > 2.0:
                    off[sid] = m.angle_deg
        print("After:  all servos read ~0" if not off else f"After:  off-zero {off}")
        if off:
            raise SystemExit(f"Calibration did not take for servos {sorted(off)} - try again.")

        LEADER_CAL_FILE.write_text(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "raw_before_deg": before,
        }, indent=2))
        print(f"Leader calibrated. Saved {LEADER_CAL_FILE.name}")
    finally:
        leader.close()


if __name__ == "__main__":
    main()
