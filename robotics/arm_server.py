"""Hold the B601-RS follower and execute commands one at a time from arm_cmd.json (for scripted / agent control).

Command file (write atomically; a new "id" triggers execution). Angles are degrees from the follower zero pose:
  {"id": 1, "op": "move", "joints": {"1": -34, "2": 99}, "speed": 15}   # joint-space smoothstep, others hold
  {"id": 2, "op": "gripper", "target": 78, "speed": 30}                 # gripper position (0 = closed)
  {"id": 3, "op": "grasp", "speed": 8, "close_torque": 1.2, "hold_torque": 0.8}
        # close slowly until the encoder shows the jaws stopped on an object, then hold lightly
  {"id": 4, "op": "path", "points": [{"1": deg, ..., "6": deg}, ...], "hz": 50}
        # play a pre-planned path continuously (motion.py); refused unless it starts at the current pose
  {"id": 5, "op": "tilt_stream", "joint": 6, "base": deg, "max_rate_deg_s": 120, "timeout_s": 0.5}
        # follow live offsets from arm_tilt.json {"seq", "deg", "done"} (adaptive pour); watchdog tilts back
  {"id": 6, "op": "rest"}                                               # ease to zero pose, disable, exit
Status is written to arm_status.json after every command and ~5x per second while busy.
Creating a file named STOP also returns to rest and exits.

Usage:  python arm_server.py [--hold-grip]   (--hold-grip: the gripper is holding an object at startup;
        keep squeezing it lightly instead of relaxing the jaws)
"""
import json
import math
import os
import sys
import time

from leader_arm import HERE, MAPPING, load_follower_zero
from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus, fine_timer
import joint_state  # console bridge: 10 Hz joint_state.json writer, no-op if too soon

CMD_FILE = HERE / "arm_cmd.json"
STATUS_FILE = HERE / "arm_status.json"
TILT_FILE = HERE / "arm_tilt.json"   # live pour-roll targets for the tilt_stream op
PATH_START_TOL_DEG = 2.0    # a path must start where the arm is commanded now
PATH_MAX_JOINT_SPEED = 90.0  # deg/s between consecutive path points; faster = refused
STOP_FILE = HERE / "STOP"
GRIPPER = 7
HZ = 100.0
GRIP_KP = 12.0           # N.m/rad, impedance gain on the gripper
GRIP_DAMPING = 1.5       # MIT kd for the gripper
GRIP_MOVE_TORQUE = 3.5   # torque limit for plain gripper moves (LeRobot RS default)
VEL_FF_MAX = 3.0         # rad/s cap on the velocity feedforward
VEL_FF_SMOOTH = 0.5      # low-pass on the feedforward (0..1, 1 = no smoothing)
CONTACT_LAG_DEG = 6.0    # jaws lagging the closing target by this much ...
CONTACT_VEL = 0.05       # ... while (nearly) stopped (rad/s) ...
CONTACT_TIME = 0.3       # ... for this long = contact
CONTACT_MIN_TRAVEL = 8.0  # ignore "contact" before the jaws have actually started closing
GRASP_SQUEEZE_DEG = 4.0  # hold target this far inside the contact point
EMPTY_GRIP_DEG = 12.0    # closing past this means nothing was in the jaws
TEMP_LIMIT_C = 80.0
FEEDBACK_TIMEOUT = 0.25
IDLE_TIMEOUT_S = 15 * 60
# follower motor-frame soft limits (degrees from zero), same as teleop
LIMITS = {mid: lim for (mid, _scale, lim) in MAPPING.values()}


def smoothstep(s):
    return s * s * (3 - 2 * s)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold-grip", action="store_true")
    ap.add_argument("--hold-torque", type=float, default=1.0)
    ap.add_argument("--idle-min", type=float, default=IDLE_TIMEOUT_S / 60,
                    help="rest and exit after this many minutes with no command (camera calibration can take long)")
    args = ap.parse_args()
    zero = load_follower_zero()
    ids = list(JOINTS)
    dt = 1.0 / HZ
    bus = RobStrideBus()
    enabled = False
    cmd = {}
    grip = {"target": 0.0, "limit": GRIP_MOVE_TORQUE}
    vff = {m: 0.0 for m in ids if m != GRIPPER}  # velocity feedforward per arm joint, rad/s
    clock = {"t": time.perf_counter(), "next": time.perf_counter(), "prev": {}, "n": 0,
             "t_rate": time.perf_counter(), "hz": None}
    fine_timer()
    last = {"id": None, "op": None, "result": "starting"}

    def clamp(mid, deg):
        lo, hi = LIMITS[mid]
        return min(hi, max(lo, deg))

    def rel(mid):
        return math.degrees(bus.state[mid]["pos"]) - zero[mid]

    def send_all():
        for mid in ids:
            if mid == GRIPPER:
                st = bus.state.get(mid)
                tau = 0.0
                if st:
                    target = math.radians(zero[mid] + grip["target"])
                    tau = max(-grip["limit"], min(grip["limit"], GRIP_KP * (target - st["pos"])))
                bus.mit(mid, 0.0, 0.0, GRIP_DAMPING, torque=tau)
            else:
                # velocity feedforward: without it the kd term brakes against every planned motion
                # (stick-slip at slow speeds); with it kd only damps the velocity ERROR
                bus.mit(mid, math.radians(zero[mid] + cmd[mid]), JOINTS[mid][1], JOINTS[mid][2], vel=vff[mid])

    def check():
        now = time.time()
        for mid in ids:
            st = bus.state.get(mid)
            if st is None or now - st["t"] > FEEDBACK_TIMEOUT:
                raise RuntimeError(f"lost feedback from J{mid}")
            if st["fault"]:
                raise RuntimeError(f"J{mid} fault 0x{st['fault']:02x}")
            if st["temp"] > TEMP_LIMIT_C:
                raise RuntimeError(f"J{mid} temperature {st['temp']:.0f} C")
        if STOP_FILE.exists():
            raise KeyboardInterrupt

    def write_status(busy=False, **extra):
        st7 = bus.state.get(GRIPPER, {})
        status = {
            "time": time.strftime("%H:%M:%S"), "busy": busy, **last,
            "command": {f"J{m}": round(cmd[m], 1) for m in ids if m != GRIPPER},
            "state": {f"J{m}": round(rel(m), 1) for m in ids if m in bus.state},
            "torque": {f"J{m}": round(bus.state[m]["torque"], 2) for m in ids if m in bus.state},
            "loop_hz": clock["hz"],
            "max_temp_c": max((s["temp"] for s in bus.state.values()), default=None),
            "gripper": {"target": round(grip["target"], 1), "torque_limit": grip["limit"],
                        "pos": round(rel(GRIPPER), 1) if st7 else None,
                        "vel_rad_s": round(st7.get("vel", 0.0), 3), "torque": round(st7.get("torque", 0.0), 2)},
            **extra,
        }
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        # Windows refuses the replace while a reader has the file open; status is best-effort, never fatal
        for _ in range(3):
            try:
                os.replace(tmp, STATUS_FILE)
                return
            except PermissionError:
                time.sleep(0.002)

    def tick():
        """One control step at a steady HZ: feedforward from how the command moved, send, check, then sleep
        until the next deadline (not a flat dt, which drifts and jitters with the time spent sending)."""
        now = time.perf_counter()
        el = now - clock["t"]
        if el > 0:
            for m in vff:
                v = math.radians(cmd[m] - clock["prev"].get(m, cmd[m])) / el
                v = max(-VEL_FF_MAX, min(VEL_FF_MAX, v))
                vff[m] += (v - vff[m]) * VEL_FF_SMOOTH
        clock["prev"], clock["t"] = dict(cmd), now
        send_all()
        joint_state.write([cmd[m] for m in range(1, 7)])  # console bridge, rate-limited to 10 Hz
        check()
        clock["next"] += dt
        wait = clock["next"] - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        elif wait < -dt:
            clock["next"] = time.perf_counter()  # fell behind (e.g. USB stall): restart the schedule
        clock["n"] += 1
        if now - clock["t_rate"] >= 1.0:
            clock["hz"], clock["n"], clock["t_rate"] = round(clock["n"] / (now - clock["t_rate"]), 1), 0, now

    def hold(seconds):
        end = time.time() + seconds
        while time.time() < end:
            tick()

    def move(goal, speed, label):
        frm = dict(cmd)
        goal = {m: clamp(m, goal.get(m, frm[m])) for m in frm}
        dist = max(abs(goal[m] - frm[m]) for m in frm)
        duration = max(0.5, 1.5 * dist / speed)
        t0 = time.time()
        t_status = 0.0
        while True:
            s = min(1.0, (time.time() - t0) / duration)
            for m in frm:
                cmd[m] = frm[m] + (goal[m] - frm[m]) * smoothstep(s)
            tick()
            if time.time() - t_status > 0.2:
                t_status = time.time()
                write_status(busy=True, progress=round(s, 2), doing=label)
            if s >= 1.0:
                return f"{label}: done in {duration:.1f}s"

    def play_path(points, hz):
        """Follow a pre-planned joint path continuously (no easing between points).
        points: [{1: deg, ..., 6: deg}, ...] spaced 1/hz apart; interpolated at the control rate."""
        arm_ids = [m for m in ids if m != GRIPPER]
        off = max(abs(points[0].get(m, cmd[m]) - cmd[m]) for m in arm_ids)
        if off > PATH_START_TOL_DEG:
            return f"path refused: first point is {off:.1f} deg from the current command (re-plan)"
        for a, b in zip(points, points[1:]):
            jump = max(abs(b.get(m, a.get(m, cmd[m])) - a.get(m, cmd[m])) for m in arm_ids)
            if jump * hz > PATH_MAX_JOINT_SPEED:
                return f"path refused: a step needs {jump * hz:.0f} deg/s (> {PATH_MAX_JOINT_SPEED:.0f})"
        pts = [{m: clamp(m, p.get(m, cmd[m])) for m in arm_ids} for p in points]
        grips = [clamp(GRIPPER, p[GRIPPER]) for p in points] if all(GRIPPER in p for p in points) else None
        if grips:
            grip["limit"] = GRIP_MOVE_TORQUE
        duration = (len(pts) - 1) / hz
        t0 = time.time()
        t_status = 0.0
        while True:
            x = min(len(pts) - 1.0, (time.time() - t0) * hz)
            i = min(int(x), len(pts) - 2) if len(pts) > 1 else 0
            f = x - i
            for m in arm_ids:
                cmd[m] = pts[i][m] if len(pts) == 1 else pts[i][m] + (pts[i + 1][m] - pts[i][m]) * f
            if grips:
                grip["target"] = grips[i] if len(grips) == 1 else grips[i] + (grips[i + 1] - grips[i]) * f
            tick()
            if time.time() - t_status > 0.2:
                t_status = time.time()
                write_status(busy=True, progress=round(x / max(1, len(pts) - 1), 2), doing="path")
            if x >= len(pts) - 1:
                return f"path: {len(pts)} points in {duration:.1f}s"

    def tilt_stream(joint, base, max_rate, timeout_s):
        """Live control of one joint (the pour roll) from TILT_FILE: {"seq": n, "deg": offset, "done": bool}.
        Offsets are relative to `base`, rate-limited, clamped to the joint limits. If the writer goes quiet
        for timeout_s, or says done, the joint returns to base and the op ends."""
        cmd[joint] = clamp(joint, cmd[joint])
        target = 0.0
        last_seq, last_t = None, time.time()
        ended = "done"
        t_status = 0.0
        while True:
            try:
                msg = json.loads(TILT_FILE.read_text())
            except (ValueError, OSError):
                msg = None
            if msg and msg.get("seq") != last_seq:
                last_seq, last_t = msg.get("seq"), time.time()
                target = float(msg.get("deg", 0.0))
                if msg.get("done"):
                    target = 0.0
                    break
            if time.time() - last_t > timeout_s:
                ended = f"watchdog: no tilt update for {timeout_s:.2f}s"
                break
            goal = clamp(joint, base + target)
            step = max_rate * dt
            cmd[joint] += max(-step, min(step, goal - cmd[joint]))
            tick()
            if time.time() - t_status > 0.2:
                t_status = time.time()
                write_status(busy=True, doing="tilt_stream", tilt_deg=round(cmd[joint] - base, 1))
        while abs(cmd[joint] - base) > 0.05:  # back to upright at the same rate limit
            step = max_rate * dt
            cmd[joint] += max(-step, min(step, base - cmd[joint]))
            tick()
        cmd[joint] = base
        return f"tilt_stream ended ({ended}), J{joint} back at {base:.1f}"

    def gripper_move(target, speed, limit=GRIP_MOVE_TORQUE):
        target = clamp(GRIPPER, target)
        grip["limit"] = limit
        frm = grip["target"]
        duration = max(0.3, abs(target - frm) / speed)
        t0 = time.time()
        while True:
            s = min(1.0, (time.time() - t0) / duration)
            grip["target"] = frm + (target - frm) * s
            tick()
            if s >= 1.0:
                break
        hold(0.5)
        return f"gripper at {rel(GRIPPER):.1f} (target {target:.1f})"

    def grasp(speed, close_torque, hold_torque):
        """Close slowly with a low torque limit; the encoder tells us when the jaws stop on something."""
        grip["limit"] = close_torque
        start_pos = rel(GRIPPER)
        grip["target"] = start_pos
        stalled_since = None
        t_status = 0.0
        while True:
            grip["target"] = max(0.0, grip["target"] - speed * dt)
            tick()
            pos = rel(GRIPPER)
            vel = abs(bus.state[GRIPPER]["vel"])
            if time.time() - t_status > 0.2:
                t_status = time.time()
                write_status(busy=True, doing="grasp")
            if pos < EMPTY_GRIP_DEG:
                grip["target"], grip["limit"] = 0.0, hold_torque
                hold(0.3)
                return {"result": "missed: jaws closed with nothing in them", "contact": False,
                        "contact_pos": None}
            lagging = pos - grip["target"] > CONTACT_LAG_DEG
            if lagging and vel < CONTACT_VEL and start_pos - pos > CONTACT_MIN_TRAVEL:
                stalled_since = stalled_since or time.time()
                if time.time() - stalled_since > CONTACT_TIME:
                    break
            else:
                stalled_since = None
        contact = rel(GRIPPER)
        grip["target"] = contact - GRASP_SQUEEZE_DEG
        grip["limit"] = hold_torque
        hold(0.5)
        held = rel(GRIPPER)
        return {"result": f"contact at {contact:.1f}, holding at {held:.1f} with <= {hold_torque} N.m",
                "contact": True, "contact_pos": round(contact, 1), "held_pos": round(held, 1),
                "slip_deg": round(contact - held, 1)}

    try:
        bus.drain(0.1)
        for mid in ids:
            pos = bus.read_param_f32(mid, P_MECH_POS, timeout=0.1)
            if pos is None:
                sys.exit(f"J{mid} did not respond - is the follower powered and on the CAN bus?")
            deg = math.degrees(pos)
            zero[mid] += 360.0 * round((deg - zero[mid]) / 360.0)  # single-turn wrap after power-up
            cmd[mid] = deg - zero[mid]
        grip["target"] = cmd.pop(GRIPPER)
        if args.hold_grip:
            grip["target"] -= GRASP_SQUEEZE_DEG
            grip["limit"] = args.hold_torque
        for mid in ids:
            bus.disable(mid)
            bus.write_param_u8(mid, P_RUN_MODE, 0)
        time.sleep(0.05)
        for mid in ids:
            bus.enable(mid)
        enabled = True
        hold(0.5)
        last["result"] = "ready, holding"
        write_status()
        print("arm_server ready", flush=True)
        STOP_FILE.unlink(missing_ok=True)

        done_id = None
        if CMD_FILE.exists():  # ignore a command left over from a previous run
            try:
                done_id = json.loads(CMD_FILE.read_text()).get("id")
            except (ValueError, OSError):
                pass
        t_idle = time.time()
        t_status = 0.0
        while True:
            c = None
            try:
                c = json.loads(CMD_FILE.read_text()) if CMD_FILE.exists() else None
            except (ValueError, OSError):
                pass  # partially written; try again next tick
            if c and c.get("id") != done_id:
                done_id = c.get("id")
                op = c.get("op")
                last.update(id=done_id, op=op, result="running")
                print(f"cmd {done_id}: {c}", flush=True)
                extra = {}
                if op == "move":
                    goal = {int(k): float(v) for k, v in c.get("joints", {}).items() if int(k) != GRIPPER}
                    last["result"] = move(goal, float(c.get("speed", 15.0)), "move")
                elif op == "gripper":
                    last["result"] = gripper_move(float(c["target"]), float(c.get("speed", 30.0)))
                elif op == "grasp":
                    extra = grasp(float(c.get("speed", 8.0)), float(c.get("close_torque", 1.2)),
                                  float(c.get("hold_torque", 0.8)))
                    last["result"] = extra.pop("result")
                elif op == "path":
                    pts = [{int(k): float(v) for k, v in p.items()} for p in c["points"]]
                    last["result"] = play_path(pts, float(c.get("hz", 50.0))) if pts else "path: empty"
                elif op == "tilt_stream":
                    j = int(c.get("joint", 6))
                    last["result"] = tilt_stream(j, float(c.get("base", cmd[j])),
                                                 float(c.get("max_rate_deg_s", 120.0)),
                                                 float(c.get("timeout_s", 0.5)))
                elif op in ("rest", "stop"):
                    raise KeyboardInterrupt
                else:
                    last["result"] = f"unknown op {op!r}"
                last["grasp"] = extra or last.get("grasp")
                print(f"  -> {last['result']}", flush=True)
                write_status()
                t_idle = time.time()
            tick()
            if time.time() - t_status > 0.5:
                t_status = time.time()
                write_status()
            if time.time() - t_idle > args.idle_min * 60:
                print(f"idle timeout: no command for {args.idle_min:.0f} min, resting and exiting", flush=True)
                last["result"] = f"idle timeout after {args.idle_min:.0f} min"
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    except Exception as e:
        last["result"] = f"STOPPED: {e!r}"
        print(last["result"], flush=True)
    finally:
        STOP_FILE.unlink(missing_ok=True)  # otherwise check() would abort the return to rest
        try:
            if enabled:
                last["op"] = "rest"
                frm = dict(cmd)
                move(dict.fromkeys(frm, 0.0), 15.0, "return to rest")
                hold(0.3)
        except BaseException as e:
            print(f"return to rest aborted: {e!r}", flush=True)
        if enabled:
            failed = []
            for mid in ids:
                try:
                    bus.disable(mid)
                except Exception:
                    failed.append(mid)
            if failed:
                msg = (f"COULD NOT DISABLE {failed} - motors still holding their last command. "
                       "Replug the CAN adapter and run rest_arm.py (support the arm before cutting power).")
            else:
                msg = "at rest, motors disabled"
            last["result"] = (last.get("result") or "") + " | " + msg
            try:
                write_status()
            except Exception:
                pass
            print(msg, flush=True)
        bus.close()
        STOP_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
