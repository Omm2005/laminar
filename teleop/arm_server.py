"""Hold the B601-RS follower and execute commands one at a time from arm_cmd.json (for scripted / agent control).

Command file (write atomically; a new "id" triggers execution). Angles are degrees from the follower zero pose:
  {"id": 1, "op": "move", "joints": {"1": -34, "2": 99}, "speed": 15}   # joint-space smoothstep, others hold
  {"id": 2, "op": "gripper", "target": 78, "speed": 30}                 # gripper position (0 = closed)
  {"id": 3, "op": "grasp", "speed": 8, "close_torque": 1.2, "hold_torque": 0.8}
        # close slowly until the encoder shows the jaws stopped on an object, then hold lightly
  {"id": 4, "op": "rest"}                                               # ease to zero pose, disable, exit
Status is written to arm_status.json after every command and ~5x per second while busy.
Creating a file named STOP also returns to rest and exits.

Usage:  python arm_server.py
"""
import json
import math
import os
import sys
import time

from leader_arm import HERE, MAPPING, load_follower_zero
from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus

CMD_FILE = HERE / "arm_cmd.json"
STATUS_FILE = HERE / "arm_status.json"
STOP_FILE = HERE / "STOP"
GRIPPER = 7
HZ = 100.0
GRIP_KP = 12.0           # N.m/rad, impedance gain on the gripper
GRIP_DAMPING = 1.5       # MIT kd for the gripper
GRIP_MOVE_TORQUE = 3.5   # torque limit for plain gripper moves (LeRobot RS default)
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
    zero = load_follower_zero()
    ids = list(JOINTS)
    dt = 1.0 / HZ
    bus = RobStrideBus()
    enabled = False
    cmd = {}
    grip = {"target": 0.0, "limit": GRIP_MOVE_TORQUE}
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
                bus.mit(mid, math.radians(zero[mid] + cmd[mid]), JOINTS[mid][1], JOINTS[mid][2])

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
        send_all()
        check()
        time.sleep(dt)

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
            if time.time() - t_idle > IDLE_TIMEOUT_S:
                print("idle timeout", flush=True)
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
            for mid in ids:
                try:
                    bus.disable(mid)
                except Exception:
                    print(f"WARNING: could not disable J{mid}", flush=True)
            last["result"] = (last.get("result") or "") + " | at rest, motors disabled"
            try:
                write_status()
            except Exception:
                pass
            print("motors disabled", flush=True)
        bus.close()
        STOP_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
