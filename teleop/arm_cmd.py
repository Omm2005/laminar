"""Send one command to arm_server.py and wait for it to finish; prints the resulting status.

Usage:
  python arm_cmd.py move 1=-34 2=99 4=88 [--speed 15]
  python arm_cmd.py gripper 78 [--speed 30]
  python arm_cmd.py grasp [--speed 8] [--close-torque 1.2] [--hold-torque 0.8]
  python arm_cmd.py rest
  python arm_cmd.py status
"""
import argparse
import json
import os
import time

from arm_server import CMD_FILE, STATUS_FILE


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except (ValueError, OSError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("op", choices=["move", "gripper", "grasp", "rest", "status"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--close-torque", type=float)
    ap.add_argument("--hold-torque", type=float)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()

    if a.op == "status":
        print(json.dumps(read_status(), indent=2))
        return

    c = {"id": time.time_ns(), "op": a.op}
    if a.op == "move":
        c["joints"] = {k: float(v) for k, v in (s.split("=") for s in a.args)}
    elif a.op == "gripper":
        c["target"] = float(a.args[0])
    for key in ("speed", "close_torque", "hold_torque"):
        if getattr(a, key) is not None:
            c[key] = getattr(a, key)

    tmp = CMD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    for _ in range(50):  # the server may be reading the command file at this instant (Windows locks it)
        try:
            os.replace(tmp, CMD_FILE)
            break
        except PermissionError:
            time.sleep(0.01)

    end = time.time() + a.timeout
    while time.time() < end:
        st = read_status()
        if st and st.get("id") == c["id"] and not st.get("busy") and st.get("result") != "running":
            break
        if a.op == "rest" and st and "motors disabled" in str(st.get("result")):
            break
        time.sleep(0.2)
    else:
        print("TIMEOUT waiting for arm_server")
    print(json.dumps(read_status(), indent=2))


if __name__ == "__main__":
    main()
