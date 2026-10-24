"""Live pour: listen for the camera's PourFrame JSON on UDP and drive the pour joint.

    python -m laminar.pour_live --target 2 --color blue                     # fake motor, prints what it would do
    python -m laminar.pour_live --target 2 --color blue --motor real --max-tilt 0.8   # motor 6 for real

Run the camera (or `python -m laminar.fake_camera`) in another terminal, sending to the same port.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from .pour import PourController, RECIPES, PourRecipe, UdpPourFeed, run_pour


class PrintMotor:
    """Stands in for the arm: prints every change of action."""

    def stream_q(self, q_list, dt):
        time.sleep(dt * len(q_list))


class Motor6:
    """Sends q[5] (joint6) to the real motor through MotorBridge."""

    def __init__(self, joint, base: float):
        self.joint, self.base = joint, base

    def stream_q(self, q_list, dt):
        for q in q_list:
            self.joint.move_to(float(q[5]), vlim=2.0)
            time.sleep(dt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, required=True, help="mL to pour")
    ap.add_argument("--color", default="blue", help="liquid the camera should see")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--motor", choices=["fake", "real"], default="fake")
    ap.add_argument("--channel", default="can0@1000000")
    ap.add_argument("--model", default="rs-00")
    ap.add_argument("--base", type=float, default=None, help="joint6 start angle, rad (default: current)")
    ap.add_argument("--max-tilt", type=float, default=None, help="cap the tilt, rad (use 0.8 for a first bench test)")
    a = ap.parse_args()

    recipe = PourRecipe(**{**RECIPES.get(a.color, PourRecipe()).__dict__})
    if a.max_tilt is not None:
        recipe.max_tilt = a.max_tilt
    ctrl = PourController(a.target, a.color, recipe)
    feed = UdpPourFeed(a.port)

    joint = None
    if a.motor == "real":
        from .motor6 import Joint
        joint = Joint(a.channel, 6, a.model)
        joint.connect()
        base = a.base if a.base is not None else joint.position()
        if base is None:
            joint.close()
            raise SystemExit("cannot read joint6 position; pass --base")
        arm = Motor6(joint, base)
    else:
        base = a.base or 0.0
        arm = PrintMotor()

    base_q = np.zeros(6)
    base_q[5] = base
    print(f"listening on udp://127.0.0.1:{a.port} | pour {a.target} mL {a.color} | motor {a.motor} | "
          f"joint6 base {base:+.3f} rad, max tilt {recipe.max_tilt} rad")
    print("waiting for camera frames ...")
    while feed.latest() is None:
        time.sleep(0.01)

    last = [None]
    orig_step = ctrl.step

    def step_and_print(frame, now, dt):
        cmd = orig_step(frame, now, dt)
        if cmd.action != last[0]:
            f = ctrl.last_frame
            ml = f"{f.ml:5.2f} mL  flow {f.flow_ml_s:4.2f}" if f else "  -  "
            print(f"  t={ctrl.elapsed:5.2f}s  {ml}  tilt {cmd.tilt:4.2f}  -> {cmd.action:<9} {cmd.reason}")
            last[0] = cmd.action
        return cmd
    ctrl.step = step_and_print

    try:
        run_pour(arm, ctrl, feed.latest, base_q, joint=5)
    except KeyboardInterrupt:
        print("interrupted: tilting back")
        arm.stream_q([base_q], 0.05)
    finally:
        feed.close()
        if joint:
            joint.close()
    print(f"result: {ctrl.state.value} - {ctrl.reason}")
    return 0 if ctrl.state.value == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
