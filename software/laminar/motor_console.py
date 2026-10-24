"""Interactive console for one motor: sweep edge to edge at several speeds, or jog it by hand.

    python -m laminar.motor_console --fake             # try it without the arm
    python -m laminar.motor_console                    # motor 6 over PCAN on a Mac
    python -m laminar.motor_console --channel can0 --model rs-00

Commands (type `help` inside):
    status                 position and raw state
    edges -0.5 0.5         sweep edges, rad
    speeds 0.3 0.6 1.0     sweep speeds, rad/s
    cycles 2               edge-to-edge round trips per speed
    sweep                  run the sweep, printing time, speed and error for each pass
    go 0.3                 move to an angle at the jog speed
    jog 0.4                set the jog speed, rad/s
    home                   to the resting position (calibrated zero, wrist upright)
    pour                   pour right, then left, into SIMULATED liquid (real wrist, no camera needed)
    pour left surge        one side; scenario: normal | surge | wrong_column | dry | overfill | blocked
    quit

Ctrl+C during a move stops it and holds where the motor is.
"""
from __future__ import annotations

import argparse
import time

from .motor6 import FakeJoint, Joint, RobStrideJoint

DEFAULT_HALF_RANGE = 0.35  # rad (20 deg) each side of where the motor starts
MARGIN = 0.10       # rad kept away from the URDF limit
MAX_SPEED = 2.0     # rad/s
REACHED = 0.03      # rad, counts as "arrived"
REST = 0.0          # resting position: the calibrated zero (robotics/follower_calibration.json)


class Console:
    def __init__(self, j: Joint):
        self.j = j
        h = DEFAULT_HALF_RANGE
        self.edges = (self._clip(REST - h), self._clip(REST + h))
        self.speeds = [0.3, 0.6, 1.0]
        self.cycles = 1
        self.jog = 0.4

    def _clip(self, x: float) -> float:
        return max(self.j.lo + MARGIN, min(self.j.hi - MARGIN, x))

    def _pos(self) -> str:
        p = self.j.position()
        return "unknown" if p is None else f"{p:+.3f} rad"

    # one move, waits until arrived or timed out, returns a result line
    def leg(self, target: float, speed: float) -> tuple[bool, str]:
        start = self.j.position()
        dist = abs(target - start) if start is not None else abs(self.edges[1] - self.edges[0])
        expected = dist / speed
        t0 = time.monotonic()
        self.j.move_to(target, speed)
        deadline = t0 + expected * 1.5 + 2.0
        pos = None
        while True:
            time.sleep(0.02)
            if getattr(self.j, "error", None):
                raise RuntimeError(f"motor stopped: {self.j.error}")
            pos = self.j.position()
            now = time.monotonic()
            if pos is None and now > t0 + expected + 0.5:
                break
            if pos is not None and abs(pos - target) < REACHED:
                break
            if now > deadline:
                break
        took = time.monotonic() - t0
        frm = "   ?  " if start is None else f"{start:+.2f}"
        if pos is None:
            return True, f"  {frm} -> {target:+.2f} @ {speed:.2f} rad/s  {took:4.1f} s  (position unreadable, timed wait)"
        err = abs(pos - target)
        ok = err < REACHED
        real_v = dist / took if took > 0 else 0.0
        return ok, (f"  {frm} -> {target:+.2f} @ {speed:.2f} rad/s  {dist:.2f} rad in {took:4.1f} s "
                    f"(actual {real_v:.2f} rad/s)  err {err:.3f}  {'OK' if ok else 'MISSED'}")

    def hold(self) -> None:
        p = self.j.position()
        if p is not None:
            self.j.move_to(p, 0.3)
        print(f"stopped, holding at {self._pos()}")

    def sweep(self) -> None:
        lo, hi = self.edges
        legs = len(self.speeds) * self.cycles * 2
        print(f"sweep {lo:+.2f} <-> {hi:+.2f} rad, speeds {self.speeds}, {self.cycles} cycle(s) each: {legs} passes")
        ok, line = self.leg(lo, min(self.speeds))
        print(line + "   (move to start edge)")
        results = []
        for v in self.speeds:
            print(f" speed {v} rad/s")
            for _ in range(self.cycles):
                for edge in (hi, lo):
                    ok, line = self.leg(edge, v)
                    results.append(ok)
                    print(line)
        print(self.leg(REST, min(self.speeds))[1] + "   (back to rest)")
        print(f"{'PASS' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} passes reached the edge")

    def pour(self, sides: list[str], scenario: str = "normal") -> None:
        """Real wrist, simulated liquid: each side starts from rest and ends back at rest."""
        from .pour_motor6 import motor_pour

        results = []
        for side in sides:
            print(self.leg(REST, self.jog)[1] + "   (to rest first)")
            print(f"\n===== POUR {side.upper()} into A (simulated liquid, scenario {scenario}) =====")
            r = motor_pour(self.j, +1 if side == "right" else -1, scenario)
            results.append((side, r.ok, f"{r.result}: {r.reason}, fill {r.final_fill:.0%} of target, "
                                        f"max tilt {r.max_tilt_deg:.0f} deg, {r.duration_s:.0f} s"))
        print(self.leg(REST, self.jog)[1] + "   (back to rest)")
        print("\nsummary:")
        for side, ok, line in results:
            print(f"  {side:<5} {'OK  ' if ok else 'FAIL'} {line}")

    def run(self) -> None:
        print(f"motor {self.j.motor_id} | URDF limits [{self.j.lo:+.2f}, {self.j.hi:+.2f}] rad | at {self._pos()}")
        print(f"edges {self.edges[0]:+.2f} {self.edges[1]:+.2f} | speeds {self.speeds} | type `help`")
        while True:
            try:
                parts = input("motor> ").split()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not parts:
                continue
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in ("q", "quit", "exit"):
                    return
                elif cmd == "help":
                    print(__doc__.split("Commands", 1)[1])
                elif cmd == "status":
                    print(f"position {self._pos()}  state {self.j.state()}")
                elif cmd == "edges":
                    lo, hi = sorted(float(x) for x in args[:2])
                    clo, chi = self._clip(lo), self._clip(hi)
                    if (clo, chi) != (lo, hi):
                        print(f"clipped to stay {MARGIN} rad inside the URDF limits")
                    self.edges = (clo, chi)
                    print(f"edges {clo:+.2f} {chi:+.2f}")
                elif cmd == "speeds":
                    self.speeds = [min(MAX_SPEED, abs(float(x))) for x in args] or self.speeds
                    print(f"speeds {self.speeds} rad/s (max {MAX_SPEED})")
                elif cmd == "cycles":
                    self.cycles = max(1, int(args[0]))
                    print(f"cycles {self.cycles}")
                elif cmd == "jog":
                    self.jog = min(MAX_SPEED, abs(float(args[0])))
                    print(f"jog speed {self.jog} rad/s")
                elif cmd == "go":
                    target = float(args[0])
                    if self._clip(target) != target:
                        print(f"refused: {target} is outside the safe range")
                        continue
                    print(self.leg(target, self.jog)[1])
                elif cmd in ("home", "rest"):
                    print(self.leg(REST, self.jog)[1] + "   (resting position)")
                elif cmd == "pour":
                    sides = [x for x in args if x in ("right", "left")] or ["right", "left"]
                    scenario = next((x for x in args if x not in ("right", "left")), "normal")
                    if input(f"pour {' then '.join(sides)} with SIMULATED liquid ({scenario}): the wrist tilts up to "
                             "85 deg each way. Arm folded at rest, area and cables clear? [y/N] ").lower() != "y":
                        continue
                    self.pour(sides, scenario)
                elif cmd == "sweep":
                    lo, hi = self.edges
                    if input(f"sweep {lo:+.2f} <-> {hi:+.2f} rad up to {max(self.speeds)} rad/s. area and cables clear? [y/N] ").lower() != "y":
                        continue
                    self.sweep()
                else:
                    print("unknown command, type `help`")
            except KeyboardInterrupt:
                print()
                self.hold()
            except (ValueError, IndexError) as e:
                print(f"bad input: {e}")
            except RuntimeError as e:
                print(f"STOPPED: {e}")
                return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="simulated motor, no hardware")
    ap.add_argument("--driver", choices=["robstride", "motorbridge"], default="robstride",
                    help="robstride = team's candleLight driver (default), motorbridge = PCAN via MotorBridge")
    ap.add_argument("--channel", default="can0@1000000", help="motorbridge only. Mac PCAN: can0@1000000, Ubuntu: can0")
    ap.add_argument("--id", type=int, default=6)
    ap.add_argument("--model", default="rs-00", help="motorbridge only: RobStride model of this joint")
    a = ap.parse_args()

    if a.fake:
        j = FakeJoint(a.id)
    elif a.driver == "robstride":
        j = RobStrideJoint(a.id)
    else:
        j = Joint(a.channel, a.id, a.model)
    print("connecting ..." if not a.fake else "fake motor")
    try:
        j.connect()
    except Exception as e:
        j.close()
        print(f"could not start motor {a.id}: {e}")
        return 1
    try:
        Console(j).run()
    finally:
        j.close()
        print("motor disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
