"""Is the motor alive? Connect, read, and (only with --move) wiggle one joint slowly and come back.

    python -m laminar.motor_test                          # read only, nothing moves
    python -m laminar.motor_test --move                   # +-0.2 rad wiggle on motor 6
    python -m laminar.motor_test --channel can0 --id 6 --model rs-00 --move --amp 0.3

Mac + PCAN: channel can0@1000000. Ubuntu SocketCAN: channel can0 (bring it up first).
"""
from __future__ import annotations

import argparse
import time

from .motor6 import Joint


def step(n: int, msg: str) -> None:
    print(f"[{n}] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="can0@1000000")
    ap.add_argument("--id", type=int, default=6, help="motor id (6 = wrist roll / pour joint)")
    ap.add_argument("--model", default="rs-00", help="RobStride model of this joint: confirm from the reBot repo")
    ap.add_argument("--move", action="store_true", help="actually move the motor")
    ap.add_argument("--amp", type=float, default=0.2, help="wiggle size, rad (max 0.5)")
    ap.add_argument("--vlim", type=float, default=0.5, help="speed limit, rad/s")
    ap.add_argument("--yes", action="store_true", help="skip the safety prompt")
    a = ap.parse_args()
    amp = min(abs(a.amp), 0.5)

    j = Joint(a.channel, a.id, a.model, vlim=a.vlim)
    step(1, f"connecting to motor {a.id} ({a.model}) on {a.channel} ...")
    try:
        j.connect()
    except SystemExit:
        raise
    except Exception as e:
        print(f"    FAIL: {type(e).__name__}: {e}")
        print("    check: 48 V on, PCAN plugged in, DIP 120R, driver installed, then run:")
        print(f"    motorbridge-cli scan --vendor robstride --channel {a.channel} --start-id 1 --end-id 7")
        return 1
    print("    OK, enabled in position mode")

    try:
        step(2, "reading state 3 times ...")
        pos = None
        for _ in range(3):
            print(f"    {j.state()}")
            pos = j.position()
            time.sleep(0.1)
        if pos is None:
            print("    could not find a position field in the state; reading works, but not moving.")
            print("    look at the printed state and set the field name in laminar/motor6.py position()")
            return 0 if not a.move else 1
        print(f"    position = {pos:+.3f} rad")

        if not a.move:
            print("done (read only). add --move to wiggle the motor.")
            return 0

        if not a.yes and input(f"[3] motor {a.id} will move +-{amp} rad at {a.vlim} rad/s. area clear? [y/N] ").lower() != "y":
            print("not moving.")
            return 0
        home, ok = pos, True
        for target in (home + amp, home, home - amp, home):
            j.move_to(target)
            time.sleep(amp / a.vlim + 0.8)
            now = j.position()
            err = abs(now - target) if now is not None else float("nan")
            good = now is not None and err < 0.05
            ok &= good
            print(f"    target {target:+.3f}  at {now if now is None else round(now, 3)}  {'OK' if good else 'FAIL'}")
        print("PASS: motor moves and comes back" if ok else "FAIL: motor did not reach its targets")
        return 0 if ok else 1
    finally:
        j.close()
        print("motor disabled")


if __name__ == "__main__":
    raise SystemExit(main())
