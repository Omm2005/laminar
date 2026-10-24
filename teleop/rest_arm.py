"""Bring the B601-RS follower slowly to its calibrated zero (rest) pose, then disable the motors.

Safe to run whether the motors are currently enabled (e.g. a crashed teleop left them holding) or not:
the first command is "hold where you are", so nothing jumps.

Usage:  python rest_arm.py [--speed DEG_PER_S]
"""
import argparse
import math
import sys
import time

from leader_arm import load_follower_zero
from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus

GRIPPER = 7


def smoothstep(s):
    return s * s * (3 - 2 * s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=15.0, help="peak joint speed in deg/s (default 15)")
    args = ap.parse_args()

    zero = load_follower_zero()
    ids = list(JOINTS)
    bus = RobStrideBus()
    enabled = False
    try:
        bus.drain(0.1)
        start = {}
        for mid in ids:
            pos = bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
            if pos is None:
                sys.exit(f"J{mid} did not respond - is the follower powered and on the CAN bus?")
            deg = math.degrees(pos)
            zero[mid] += 360.0 * round((deg - zero[mid]) / 360.0)  # single-turn wrap after power-up
            start[mid] = deg
        print("From:", "  ".join(f"J{m}={start[m] - zero[m]:6.1f}" for m in ids), "(deg from rest)")

        for mid in ids:
            bus.disable(mid)
            bus.write_param_u8(mid, P_RUN_MODE, 0)  # MIT mode
        time.sleep(0.05)
        cmd = dict(start)

        def send_all():
            for mid in ids:
                kp, kd = JOINTS[mid][1], JOINTS[mid][2]
                if mid == GRIPPER:
                    kp = 12.0  # gentle on the gripper
                bus.mit(mid, math.radians(cmd[mid]), kp, kd)
            bus.drain(0.0)

        for mid in ids:
            bus.enable(mid)
        enabled = True
        t_end = time.time() + 0.3
        while time.time() < t_end:
            send_all()
            time.sleep(0.01)

        dist = max(abs(zero[m] - start[m]) for m in ids)
        duration = max(1.5, 1.5 * dist / args.speed)
        print(f"Returning to rest ({duration:.1f} s)")
        t0 = time.time()
        while True:
            s = min(1.0, (time.time() - t0) / duration)
            for m in ids:
                cmd[m] = start[m] + (zero[m] - start[m]) * smoothstep(s)
            send_all()
            if s >= 1.0:
                break
            time.sleep(0.01)
        t_end = time.time() + 0.5
        while time.time() < t_end:
            send_all()
            time.sleep(0.01)
        print("At rest:", "  ".join(f"J{m}={math.degrees(bus.state[m]['pos']) - zero[m]:6.1f}"
                                    for m in ids if m in bus.state))
    finally:
        if enabled:
            for mid in ids:
                try:
                    bus.disable(mid)
                except Exception:
                    print(f"WARNING: could not disable J{mid}")
            print("Motors disabled.")
        bus.close()


if __name__ == "__main__":
    main()
