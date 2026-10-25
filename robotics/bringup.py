"""Bring the arm up reliably, retrying the whole CAN side when it misbehaves: python bringup.py [--tries 3]

Each try:
    1. stop any arm_server.py
    2. reset the CAN side: open the USB-CAN adapter fresh, switch every motor off (disable with fault clear, run
       mode 0), close the adapter, wait
    3. start arm_server.py (log: logs/arm_server.log) and wait for "arm_server ready"
    4. check it: feedback stays live for HEALTHY_S, and the gripper really opens and closes
A failure anywhere (lost feedback, a gripper that doesn't move, the server exiting) -> stop it, motors off, wait,
try again. Exit 0 with arm_server.py running and healthy, 1 if every try failed.

Motors are only switched off with the arm at rest: off in the air, the arm would fall. Away from rest it refuses.
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "logs" / "arm_server.log"
READY_S = 20.0          # arm_server must say ready within this
HEALTHY_S = 3.0         # ... and keep running with live feedback this long
GRIP_TEST_DEG = 20.0    # the gripper must open to within GRIP_TOL_DEG of this, and close again
GRIP_TOL_DEG = 5.0
REST_TOL_DEG = 12.0     # joints 1-5 this close to the zero pose = at rest (motors may be switched off)
SETTLE_S = 3.0          # after motors off / adapter closed: Windows needs a moment to release the USB device
OPEN_TRIES = 4          # opening the adapter right after another process closed it can fail (access denied)


def servers():
    import psutil
    return [p for p in psutil.process_iter(["pid", "cmdline"])
            if any(c.endswith("arm_server.py") for c in (p.info.get("cmdline") or []))]


def stop_servers() -> None:
    for p in servers():
        print(f"  stopping arm_server.py (pid {p.pid})")
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
    (HERE / "arm_cmd.json").unlink(missing_ok=True)   # never let a stale command run on the next server


def reset_can() -> None:
    """Fresh adapter, every motor off. Raises if the arm is not at rest (switching off would drop it)."""
    from leader_arm import load_follower_zero
    from robstride_can import JOINTS, P_MECH_POS, P_RUN_MODE, RobStrideBus
    zero = load_follower_zero()
    bus = None
    for k in range(OPEN_TRIES):
        try:
            bus = RobStrideBus()
            break
        except Exception as e:  # noqa: BLE001 - USBError, access violation, "Cannot find device"
            if k == OPEN_TRIES - 1:
                raise RuntimeError(f"USB-CAN adapter would not open: {e!r}") from None
            time.sleep(SETTLE_S)
    try:
        bus.drain(0.1)
        pos, missing = {}, []
        for m in JOINTS:
            v = bus.read_param_f32(m, P_MECH_POS, timeout=0.2)
            if v is None:
                missing.append(m)
            else:
                d = math.degrees(v) - zero[m]
                pos[m] = d - 360.0 * round(d / 360.0)
        if missing:
            raise RuntimeError(f"motors not answering: {missing} (48 V off, or a loose CAN cable?)")
        away = {m: round(pos[m], 1) for m in range(1, 6) if abs(pos[m]) > REST_TOL_DEG}
        if away:
            raise SystemExit(f"NOT switching motors off: the arm is not at rest {away} - it would fall. "
                             "Bring it to rest first (python rest_arm.py) or support it.")
        for m in JOINTS:
            bus.disable(m, clear_fault=True)
            time.sleep(0.01)
            bus.write_param_u8(m, P_RUN_MODE, 0)
            time.sleep(0.01)
        print("  CAN reset: every motor off, faults cleared, adapter reopened")
    finally:
        bus.close()
    time.sleep(SETTLE_S)


def start_server() -> subprocess.Popen:
    LOG.parent.mkdir(exist_ok=True)
    f = open(LOG, "w")
    p = subprocess.Popen([sys.executable, "-u", str(HERE / "arm_server.py")], cwd=str(HERE), stdout=f,
                         stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    end = time.time() + READY_S
    while time.time() < end:
        txt = LOG.read_text(errors="replace")
        if "arm_server ready" in txt:
            return p
        if p.poll() is not None:
            raise RuntimeError("arm_server exited at startup: " + _last(txt))
        time.sleep(0.2)
    raise RuntimeError("arm_server not ready within %g s" % READY_S)


def _last(txt: str) -> str:
    lines = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith(("cmd ", "[tiger]"))]
    return " | ".join(lines[-3:])


def check(p: subprocess.Popen) -> None:
    from arm_client import ArmClient
    t_end = time.time() + HEALTHY_S
    while time.time() < t_end:
        if p.poll() is not None:
            raise RuntimeError("arm_server stopped: " + _last(LOG.read_text(errors="replace")))
        time.sleep(0.2)
    c = ArmClient()
    c._send({"op": "gripper", "target": GRIP_TEST_DEG, "speed": 60.0})
    g = (c.status() or {}).get("gripper") or {}
    if abs(float(g.get("pos", -99)) - GRIP_TEST_DEG) > GRIP_TOL_DEG:
        raise RuntimeError(f"gripper did not open: at {g.get('pos')} for {GRIP_TEST_DEG:g} (torque {g.get('torque')}, "
                           f"mode {g.get('mode')}, fault {g.get('fault')})")
    c._send({"op": "gripper", "target": 0.0, "speed": 60.0})
    if p.poll() is not None:
        raise RuntimeError("arm_server stopped: " + _last(LOG.read_text(errors="replace")))
    print(f"  healthy: feedback live for {HEALTHY_S:g} s, gripper opened to {g.get('pos')} and closed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tries", type=int, default=3)
    a = ap.parse_args()
    for k in range(1, a.tries + 1):
        print(f"bring-up try {k}/{a.tries}")
        stop_servers()
        try:
            reset_can()
            p = start_server()
            check(p)
            print("arm_server.py is running and healthy (log: logs/arm_server.log)")
            return 0
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - any failure = stop, motors off, wait, next try
            print(f"  FAILED: {e!r}" if not isinstance(e, RuntimeError) else f"  FAILED: {e}")
            stop_servers()
            time.sleep(SETTLE_S)
    print("every bring-up try failed. If it says the adapter would not open / access denied / access violation, the "
          "USB-CAN adapter is wedged: unplug its USB, wait 5 s, plug it back in (no software reset frees it). "
          "Otherwise check the 48 V supply and the CAN cables (base adapter, J5-J6-J7 wrist).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
