"""Teleoperate the reBot Arm B601-RS follower with the FashionStar reBot Arm 102 leader.

Startup: the follower holds its current pose, then eases (smoothstep, --ramp-speed) onto the leader's pose,
then tracks the leader live with a per-joint speed limit (--max-speed).
Stop: press q / Esc / Ctrl+C (or create a file named STOP next to this script). The follower then
returns slowly to its zero (resting) pose and the motors are disabled.

Requires calibrate_leader.py and calibrate_follower.py to have been run with both arms in the zero pose.

Usage:  python teleop.py [--hz 100] [--max-speed 180] [--ramp-speed 20] [--duration SECONDS]
                         [--record] [--fps 20] [--camera 2] [--width 640] [--height 480]
--record saves camera video + joint data to recordings/<timestamp>/ (see recorder.py).
"""
import argparse
import json
import math
import os
import sys
import threading
import time

from leader_arm import HERE, LEADER_CAL_FILE, MAPPING, LeaderArm, load_follower_zero
from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus, boost_process, fine_timer
# the same joint control as arm_server.py (velocity feedforward, J1 gains + friction, fitted compensation): teleop
# used bare kp/kd, so kd braked against every motion and the follower lagged, then lurched (2026-10-25)
from arm_server import FRICTION_FF_NM, FRICTION_FF_VEL, GAIN_OVERRIDE, VEL_FF_MAX, VEL_FF_SMOOTH
from recorder import Camera, Recorder

try:
    import msvcrt
except ImportError:
    msvcrt = None

GRIPPER = 7
RUN_MODE = 2             # RobStride feedback mode field: 0 reset, 1 calibration, 2 running
# torque-limited impedance for the gripper so it can't crush things (values from LeRobot's RS profile)
GRIP_KP = 12.0          # N.m / rad
GRIP_DAMPING = 1.5      # MIT kd
GRIP_TORQUE_MOVING = 3.5
GRIP_TORQUE_HOLDING = 1.0
GRIP_HOLD_VEL = 0.25    # rad/s
GRIP_STALL_S = 0.4      # jaws far from target but not moving for this long = closed on something: hold gently
GRIP_NEAR_DEG = 3.0     # within this of the target counts as arrived (holding torque)
STOP_FILE = HERE / "STOP"
STATE_FILE = HERE / "teleop_state.json"  # measured follower joints at 10 Hz, for markers.py calibrate
FEEDBACK_TIMEOUT = 0.25  # s without feedback from a follower motor -> stop
LEADER_TIMEOUT = 0.5     # s without leader readings -> return to zero
TEMP_LIMIT_C = 80.0


def smoothstep(s):
    return s * s * (3 - 2 * s)


def stop_requested():
    if STOP_FILE.exists():
        return True
    if msvcrt and msvcrt.kbhit():
        return msvcrt.getwch().lower() in ("q", "\x1b", "\r")
    return False


def main():
    ap = argparse.ArgumentParser()
    # 100 Hz keeps the candleLight adapter's TX queue well below saturation (7 commands per tick)
    ap.add_argument("--hz", type=float, default=100.0, help="control loop rate")
    ap.add_argument("--max-speed", type=float, default=180.0, help="follower joint speed limit while tracking, deg/s")
    ap.add_argument("--smooth-ms", type=float, default=60.0,
                    help="low-pass time constant on the leader's angles, ms (0 = raw; the leader updates in steps)")
    ap.add_argument("--keep-grip", action="store_true",
                    help="on stop, return the arm to rest but leave the gripper where it is")
    ap.add_argument("--ramp-speed", type=float, default=20.0, help="speed when easing onto the leader / back to zero, deg/s")
    ap.add_argument("--duration", type=float, default=None, help="stop automatically after this many seconds")
    ap.add_argument("--max-start-mismatch", type=float, default=25.0,
                    help="refuse to start if any joint differs between leader and follower by more than this (deg)")
    ap.add_argument("--leader-port", default=None, help="leader serial port (default: the CH340 adapter, auto-detected)")
    ap.add_argument("--record", action="store_true", help="record camera video + joint data")
    ap.add_argument("--fps", type=float, default=20.0, help="recording frame rate")
    ap.add_argument("--camera", type=int, default=2, help="camera index (2 = Innomaker U20CAM on this laptop; -1 = joint data only, no video)")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()

    if not LEADER_CAL_FILE.exists():
        sys.exit("Leader not calibrated - run: python calibrate_leader.py")
    zero = load_follower_zero()
    STOP_FILE.unlink(missing_ok=True)
    ids = list(JOINTS)
    dt = 1.0 / args.hz
    fine_timer()  # 1 ms sleeps on Windows, so --hz is actually reached
    boost_process()  # high priority, no power throttling: the loop was wandering 45-94 Hz
    comp = None
    if (HERE / "compensation.json").exists():
        from compensation import Compensation
        comp = Compensation.load(HERE / "compensation.json")

    camera = Camera(args.camera, args.width, args.height) if args.record and args.camera >= 0 else None
    recorder = None
    leader = LeaderArm(args.leader_port)
    bus = RobStrideBus()
    enabled = False
    cmd = {}  # follower targets, degrees relative to the follower zero pose
    regrip = {"t": 0.0}  # last time the gripper was re-enabled
    grip_stall = {"since": 0.0, "target": 0.0}   # when the jaws last moved / got a new target
    smooth: dict[int, float] = {}                # low-passed leader goal (--smooth-ms)

    vff = {m: 0.0 for m in ids if m != GRIPPER}   # velocity feedforward, rad/s (from how cmd moved)
    ff_clock = {"t": time.perf_counter(), "prev": {}}

    def send_all():
        now_p = time.perf_counter()
        el = now_p - ff_clock["t"]
        if el > 0:
            for m in vff:
                v = math.radians(cmd[m] - ff_clock["prev"].get(m, cmd[m])) / el
                vff[m] += (max(-VEL_FF_MAX, min(VEL_FF_MAX, v)) - vff[m]) * VEL_FF_SMOOTH
        ff_clock["t"], ff_clock["prev"] = now_p, dict(cmd)
        ff = comp.feedforward({m: cmd[m] for m in vff}, vff) if comp else {}
        with bus.send_batch():  # all 7 commands under one hold of the USB lock
            for mid in ids:
                target = math.radians(zero[mid] + cmd[mid])
                if mid == GRIPPER:
                    st = bus.state.get(mid)
                    tau = 0.0
                    if st:
                        # Full torque to get the jaws moving and keep them moving; the gentle holding torque only
                        # once they've arrived, or have been stalled away from the target for GRIP_STALL_S (closed
                        # on a tube). Holding torque whenever the jaws were still made a deadlock: 1 N.m doesn't
                        # break the jaws' static friction, so they never started moving and never got full torque.
                        now_g = time.time()
                        far = abs(math.degrees(target - st["pos"])) > GRIP_NEAR_DEG
                        if abs(st["vel"]) > GRIP_HOLD_VEL or abs(cmd[mid] - grip_stall["target"]) > GRIP_NEAR_DEG:
                            grip_stall.update(since=now_g, target=cmd[mid])   # moving, or a new target: not stalled
                        stalled = now_g - grip_stall["since"] > GRIP_STALL_S
                        limit = GRIP_TORQUE_MOVING if far and not stalled else GRIP_TORQUE_HOLDING
                        tau = max(-limit, min(limit, GRIP_KP * (target - st["pos"])))
                    bus.mit(mid, 0.0, 0.0, GRIP_DAMPING, torque=tau)
                else:
                    kp, kd = GAIN_OVERRIDE.get(mid, JOINTS[mid][1:3])
                    if comp and (mid in comp.friction_nm or mid in comp.gravity_scale):
                        tau = ff[mid]
                    else:
                        tau = FRICTION_FF_NM.get(mid, 0.0) * math.tanh(vff[mid] / FRICTION_FF_VEL) + ff.get(mid, 0.0)
                    bus.mit(mid, target, kp, kd, vel=vff[mid], torque=tau)
        bus.drain(0.0)

    def check_follower():
        now = time.time()
        for mid in ids:
            st = bus.state.get(mid)
            if st is None or now - st["t"] > FEEDBACK_TIMEOUT:
                raise RuntimeError(f"lost feedback from follower J{mid}")
            if st["fault"]:
                raise RuntimeError(f"follower J{mid} fault 0x{st['fault']:02x}")
            if st["temp"] > TEMP_LIMIT_C:
                raise RuntimeError(f"follower J{mid} temperature {st['temp']:.0f} C")
            # a gripper that drops out of run mode (mode 2) takes commands but applies no torque: re-enable it
            if mid == GRIPPER and st.get("mode") != RUN_MODE and now - regrip["t"] > 1.0:
                regrip["t"] = now
                bus.enable(mid)
                print(f"\ngripper was in mode {st.get('mode')}, not running: re-enabled", flush=True)

    def ease_to(get_goal, label):
        """Smoothstep from the current command to a (possibly moving) goal at --ramp-speed."""
        frm = dict(cmd)
        goal = get_goal()
        duration = max(1.0, 1.5 * max(abs(goal[m] - frm[m]) for m in ids) / args.ramp_speed)
        print(f"{label} ({duration:.1f} s)")
        t0 = time.time()
        while True:
            s = min(1.0, (time.time() - t0) / duration)
            goal = get_goal()
            for m in ids:
                cmd[m] = frm[m] + (goal[m] - frm[m]) * smoothstep(s)
            send_all()
            if s >= 1.0:
                return
            time.sleep(dt)

    try:
        # current follower pose, relative to its zero pose
        bus.drain(0.1)
        for mid in ids:
            pos = bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
            if pos is None:
                sys.exit(f"Follower J{mid} did not respond - is it powered and on the CAN bus?")
            # RobStride reports a single-turn angle after power-up, so the saved zero may be a full turn
            # away from the current reading (e.g. +178.6 saved vs -179.8 now); use the nearest equivalent.
            zero[mid] += 360.0 * round((math.degrees(pos) - zero[mid]) / 360.0)
            cmd[mid] = math.degrees(pos) - zero[mid]
        start_leader = leader.read_follower_targets()
        print("Follower:", "  ".join(f"J{m}={cmd[m]:6.1f}" for m in ids))
        print("Leader:  ", "  ".join(f"J{m}={a:6.1f}" for m, a in start_leader.items()))
        # Refuse to start if the arms disagree a lot: either the leader isn't posed like the follower or a
        # calibration is off, and easing would swing the follower a long way (gripper allowed more: 6x scale).
        mismatch = {m: start_leader[m] - cmd[m] for m in ids
                    if abs(start_leader[m] - cmd[m]) > args.max_start_mismatch * (6.0 if m == GRIPPER else 1.0)}
        if mismatch:
            sys.exit("Not starting - leader and follower poses differ by "
                     + ", ".join(f"J{m} {d:+.0f} deg" for m, d in mismatch.items())
                     + ". Pose the leader like the follower (or recalibrate), then retry.")

        for mid in ids:
            bus.disable(mid)
            bus.write_param_u8(mid, P_RUN_MODE, 0)  # MIT mode
        time.sleep(0.05)
        for mid in ids:
            bus.enable(mid)
        enabled = True
        # an enable frame can be lost (seen on the gripper after a power cycle: it then takes commands but applies
        # no torque). Hold, check every motor reports run mode, re-send the enable to any that don't.
        for attempt in range(3):
            t_end = time.time() + 0.5
            while time.time() < t_end:
                send_all()
                time.sleep(dt)
            idle = [m for m in ids if bus.state.get(m, {}).get("mode") != RUN_MODE]
            if not idle:
                break
            print(f"motors not running yet: {['J%d' % m for m in idle]} - enabling again")
            for mid in idle:
                bus.enable(mid)
        else:
            raise RuntimeError(f"motors would not enter run mode: {['J%d' % m for m in idle]}")
        check_follower()

        last_leader = leader.read_follower_targets()
        last_leader_t = time.time()

        # the leader is read on its own thread: a read is ~4 ms, but a missed reply waits ~110 ms for the serial
        # timeout, and done inline that froze the whole control loop (the follower jerked)
        lead = {"q": last_leader, "t": last_leader_t, "ms": [], "run": True}

        def leader_reader():
            while lead["run"]:
                t_l = time.perf_counter()
                try:
                    q = leader.read_follower_targets()
                except RuntimeError:
                    time.sleep(0.002)
                    continue
                lead["q"], lead["t"] = q, time.time()
                lead["ms"].append((time.perf_counter() - t_l) * 1000.0)
                time.sleep(0.001)

        reader = threading.Thread(target=leader_reader, daemon=True)
        reader.start()

        def leader_goal():
            nonlocal last_leader, last_leader_t
            last_leader, last_leader_t = lead["q"], lead["t"]
            return last_leader

        ease_to(leader_goal, "Easing follower onto leader pose")
        if args.record:
            def sample():
                state = {m: math.degrees(st["pos"]) - zero[m] for m, st in list(bus.state.items())}
                return dict(last_leader), dict(cmd), state
            recorder = Recorder(camera, sample, args.fps, {"camera_index": args.camera, "follower_zero_deg": zero})
            recorder.start()
            what = f"{camera.shape[1]}x{camera.shape[0]} video + joints" if camera else "joints only (no camera)"
            print(f"Recording {what} @ {args.fps:g} fps to {recorder.dir}")
        print("Teleop running - press q (or Esc/Ctrl+C) to stop.")

        t_start = time.time()
        t_print = t_state = 0.0
        t_prev = time.perf_counter()
        loops = 0
        gaps = []          # loop periods, ms: summarised at the end
        send_ms = []       # time to send the 7 motor commands, ms
        deadline = time.perf_counter()
        while not stop_requested():
            if args.duration is not None and time.time() - t_start > args.duration:
                break
            t0 = time.perf_counter()
            gaps.append((t0 - t_prev) * 1000.0)
            elapsed, t_prev = min(t0 - t_prev, 0.05), t0  # speed cap uses real loop time
            goal = leader_goal()
            if time.time() - last_leader_t > LEADER_TIMEOUT:
                print("\nLost the leader arm - stopping.")
                break
            # The leader's angles arrive as steps (held for a few loops, then a jump), which the follower
            # reproduced as hold-jump-hold. A first-order low-pass spreads each step over ~smooth_ms.
            if args.smooth_ms > 0:
                a = min(1.0, elapsed / (args.smooth_ms / 1000.0))
                for m in ids:
                    smooth[m] = goal[m] if m not in smooth else smooth[m] + a * (goal[m] - smooth[m])
                goal = dict(smooth)
            for m in ids:
                step = args.max_speed * elapsed * (6.0 if m == GRIPPER else 1.0)  # gripper travels 6x the leader angle
                cmd[m] += max(-step, min(step, goal[m] - cmd[m]))
            t_s = time.perf_counter()
            send_all()
            send_ms.append((time.perf_counter() - t_s) * 1000.0)
            check_follower()
            loops += 1
            now = time.time()
            if now - t_state > 0.1:  # best effort: a locked file never interrupts teleop
                t_state = now
                try:
                    STATE_FILE.with_suffix(".tmp").write_text(json.dumps({"t": now, "joints": {
                        m: round(math.degrees(st["pos"]) - zero[m], 3) for m, st in list(bus.state.items())}}))
                    os.replace(STATE_FILE.with_suffix(".tmp"), STATE_FILE)
                except OSError:
                    pass
            if now - t_print > 0.5:
                rate = loops / (now - t_print) if t_print else 0.0
                t_print, loops = now, 0
                temps = max(st["temp"] for st in bus.state.values())
                line = "  ".join(f"J{m}={cmd[m]:6.1f}" for m in ids)
                g = bus.state.get(GRIPPER)
                grip = f"grip leader {goal[GRIPPER]:5.1f} jaws {math.degrees(g['pos']) - zero[GRIPPER]:5.1f}" if g else ""
                print(f"\r{line}  {grip}  max {temps:.0f}C  {rate:5.0f} Hz ", end="", flush=True)
            # sleep to the next fixed deadline (as arm_server.py): a flat dt-minus-work sleep drifted to ~93 Hz
            deadline += dt
            wait = deadline - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            elif wait < -dt:
                deadline = time.perf_counter()  # fell behind: restart the schedule instead of bursting
        print()
        if len(gaps) > 10:
            g, lm = sorted(gaps[1:]), sorted(lead["ms"]) or [0.0]
            print(f"loop: {1000.0 / g[len(g) // 2]:.0f} Hz typical, period p95 {g[int(0.95 * len(g))]:.1f} ms, "
                  f"max {g[-1]:.0f} ms, {sum(x > 20 for x in g)} periods over 20 ms; leader read median "
                  f"{lm[len(lm) // 2]:.1f} ms, max {lm[-1]:.0f} ms; motor send median "
                  f"{sorted(send_ms)[len(send_ms) // 2]:.1f} ms, max {max(send_ms):.0f} ms")
    except KeyboardInterrupt:
        print()
    except Exception as e:  # leader/follower errors and CAN adapter failures all end the session safely
        print(f"\nSTOPPING: {e!r}")
    finally:
        at_rest = not enabled
        try:
            if enabled:
                rest = dict.fromkeys(ids, 0.0)
                if args.keep_grip:   # arm to rest, jaws stay where they are (e.g. still holding a tube)
                    rest[GRIPPER] = cmd[GRIPPER]
                ease_to(lambda: rest, "Returning follower to zero pose"
                        + (" (gripper kept at %.0f deg)" % cmd[GRIPPER] if args.keep_grip else ""))
                t_end = time.time() + 0.3
                while time.time() < t_end:
                    send_all()
                    time.sleep(dt)
                at_rest = True
        except BaseException as e:
            print(f"Return to zero aborted: {e!r}")
        finally:
            if enabled and not at_rest:  # never drop the arm: the motors keep holding their last command
                print("NOT AT REST - follower motors left HOLDING their last position (not disabled). Support the "
                      "arm, then run rest_arm.py.")
            elif enabled:
                failed = []
                for mid in ids:
                    try:
                        bus.disable(mid)
                    except Exception:
                        failed.append(mid)
                if failed:
                    print(f"WARNING: could not disable follower motors {failed} - they may still be holding. "
                          "Support the arm and cut the 48 V supply, or replug the CAN adapter and run teleop again.")
                else:
                    print("Follower motors disabled.")
            if recorder:
                try:
                    out = recorder.stop()
                    print(f"Saved {recorder.frames} frames ({recorder.frames / args.fps:.1f} s) to {out}"
                          + (f" - {recorder.late} frames written late" if recorder.late else ""))
                except Exception as e:
                    print(f"Recording finalize failed: {e!r}")
            if "lead" in locals():
                lead["run"] = False
                reader.join(timeout=0.5)
            for closer in (bus.close, leader.close) + ((camera.close,) if camera else ()):
                try:
                    closer()
                except Exception:
                    pass
            STOP_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
