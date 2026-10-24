"""reBot Arm B601-RS demo:
  1. rotate the base (J1) by +30 deg
  2. rotate every actuator (J1-J6 + gripper J7) by a further 30 deg (+30, or -30 where +30 would exceed a limit)
  3. hold, then return to the starting pose and disable

Moves are relative to the pose the arm is in when the script starts, use smoothstep trajectories
in MIT mode (Seeed's default gains), and are refused if any target falls outside the joint limits.
Ctrl+C at any point: returns to the starting pose, then disables. Ctrl+C again: disable immediately (arm drops!).

Usage:  python move_arm.py [--speed DEG_PER_S] [--hold SECONDS] [--dry-run]
"""
import argparse
import math
import sys
import time

from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus

STEP_DEG = 30.0
CONTROL_HZ = 200.0
LIMIT_SLACK_DEG = 3.0  # the resting pose can sit slightly outside the nominal limits


def smoothstep(s):
    return s * s * (3 - 2 * s)


def in_limits(mid, deg):
    return JOINTS[mid][3] - LIMIT_SLACK_DEG <= deg <= JOINTS[mid][4] + LIMIT_SLACK_DEG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=15.0, help="peak joint speed in deg/s (default 15)")
    ap.add_argument("--hold", type=float, default=3.0, help="seconds to hold the final pose before returning (default 3)")
    ap.add_argument("--dry-run", action="store_true", help="read positions and print the plan without enabling motors")
    args = ap.parse_args()

    bus = RobStrideBus()
    ids = list(JOINTS)
    enabled = False
    cmd = {}
    try:
        bus.drain(0.1)
        start = {}
        for mid in ids:
            pos = bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
            if pos is None:
                sys.exit(f"J{mid} did not respond - is the arm powered (48 V) and the CAN cable connected?")
            start[mid] = math.degrees(pos)

        # waypoints in degrees: start -> base +30 -> everything 30 more
        wp1 = dict(start)
        wp1[1] += STEP_DEG
        wp2 = {}
        for mid in ids:
            wp2[mid] = wp1[mid] + STEP_DEG
            if not in_limits(mid, wp2[mid]) and in_limits(mid, wp1[mid] - STEP_DEG):
                wp2[mid] = wp1[mid] - STEP_DEG

        print(" joint   start    step1    step2   limits")
        bad = []
        for mid in ids:
            lo, hi = JOINTS[mid][3], JOINTS[mid][4]
            print(f"  J{mid}  {start[mid]:7.1f}  {wp1[mid]:7.1f}  {wp2[mid]:7.1f}   [{lo:.0f}, {hi:.0f}]")
            if not (in_limits(mid, wp1[mid]) and in_limits(mid, wp2[mid])):
                bad.append(mid)
        if bad:
            sys.exit(f"Refusing to move: targets for {', '.join(f'J{m}' for m in bad)} exceed joint limits.")
        if args.dry_run:
            print("Dry run - no motors enabled.")
            return

        # MIT mode (run_mode 0) must be set while disabled, then enable
        for mid in ids:
            bus.disable(mid)
            bus.write_param_u8(mid, P_RUN_MODE, 0)
        time.sleep(0.05)
        cmd.update({mid: math.radians(start[mid]) for mid in ids})

        def send_all():
            for mid in ids:
                bus.mit(mid, cmd[mid], JOINTS[mid][1], JOINTS[mid][2])
            bus.drain(0.0)

        def hold(seconds):
            end = time.time() + seconds
            while time.time() < end:
                send_all()
                time.sleep(1 / CONTROL_HZ)

        def move(frm, to, label):
            dist = max(abs(to[m] - frm[m]) for m in ids)
            duration = max(1.0, 1.5 * dist / args.speed)  # smoothstep peak speed = 1.5x average
            print(f"{label}: {duration:.1f} s")
            t0 = time.time()
            while True:
                s = min(1.0, (time.time() - t0) / duration)
                k = smoothstep(s)
                for m in ids:
                    cmd[m] = math.radians(frm[m] + (to[m] - frm[m]) * k)
                send_all()
                if s >= 1.0:
                    break
                time.sleep(1 / CONTROL_HZ)

        for mid in ids:
            bus.enable(mid)
        enabled = True
        send_all()  # immediately command "hold where you are" so nothing jumps
        hold(0.5)

        missing = [m for m in ids if m not in bus.state]
        faulted = [m for m in ids if bus.state.get(m, {}).get("fault")]
        if missing or faulted:
            print(f"Aborting: no feedback from {missing}, faults on {faulted}")
            return

        move(start, wp1, "Step 1: base +30 deg")
        hold(1.0)
        move(wp1, wp2, "Step 2: all joints 30 deg")
        hold(0.5)

        print("Reached pose:")
        for mid in ids:
            st = bus.state.get(mid)
            if st:
                print(f"  J{mid}: target {wp2[mid]:7.1f}  actual {math.degrees(st['pos']):7.1f} deg"
                      f"  temp {st['temp']:.0f} C  fault 0x{st['fault']:02x}")

        hold(args.hold)
        move(wp2, start, "Returning to start pose")
        hold(0.5)
    except KeyboardInterrupt:
        if enabled:
            print("\nInterrupted - returning to start pose (Ctrl+C again to drop immediately).")
            try:
                frm = {m: math.degrees(cmd[m]) for m in ids}
                move(frm, start, "Returning to start pose")
                hold(0.5)
            except KeyboardInterrupt:
                pass
    finally:
        if enabled:
            for mid in ids:
                bus.disable(mid)
            print("Motors disabled.")
        bus.close()


if __name__ == "__main__":
    main()
