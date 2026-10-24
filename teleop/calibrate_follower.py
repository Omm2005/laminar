"""Record the B601-RS follower's zero pose (passive: motors are never enabled, nothing is written to them).

Put the follower in its resting zero pose (arm folded down, gripper closed) before running.
The positions are saved to follower_calibration.json and used by teleop.py as the follower's zero.

Usage:  python calibrate_follower.py [--yes]
"""
import argparse
import json
import math
import time

from leader_arm import FOLLOWER_CAL_FILE
from robstride_can import JOINTS, P_MECH_POS, RobStrideBus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="don't wait for ENTER (arm is already in the zero pose)")
    args = ap.parse_args()
    if not args.yes:
        input("Put the follower in its zero (resting) pose with the gripper closed, then press ENTER...")

    bus = RobStrideBus()
    try:
        bus.drain(0.1)
        zero = {}
        for mid in JOINTS:
            pos = bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
            if pos is None:
                raise SystemExit(f"J{mid} did not respond - is the follower powered and on the CAN bus?")
            zero[mid] = math.degrees(pos)
            print(f"J{mid}: {zero[mid]:8.2f} deg")
    finally:
        bus.close()

    FOLLOWER_CAL_FILE.write_text(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "zero_deg": zero}, indent=2))
    print(f"Follower zero pose saved to {FOLLOWER_CAL_FILE.name}")


if __name__ == "__main__":
    main()
