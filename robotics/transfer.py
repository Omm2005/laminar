"""Vial -> compartment -> vial round trip with an adaptive pour, from the pose a grab ends in.

    lift 100 -> shift (dx, dy) in that locked horizontal plane -> lower 60
    -> adaptive pour on the wrist roll (software/laminar/pour.py PourController, camera frames over UDP)
    -> lift 60 -> shift (-dx, -dy) -> lower 100

(dx, dy) comes from layout.yaml (the only bench measurements needed). All six moves are planned before
the arm moves; each is played continuously by arm_server.py ("path" op). The grab before and the
release after are separate (recorded) steps.

Run from robotics/ with arm_server.py running and the tube already gripped in its slot:

    python transfer.py vial_A well_2                                  # plan only, print the six moves
    python transfer.py vial_A well_2 --ml 2 --liquid blue --run       # camera-driven pour
    python transfer.py vial_A well_2 --pour fixed --tilt 80 --hold 3 --run   # no camera: fixed tilt
    python transfer.py vial_A well_2 --pour none --run                # moves only, no pour
"""
from __future__ import annotations

import argparse
import dataclasses
import math
import sys
import time
from pathlib import Path as FilePath

import yaml

import motion as M
from arm_client import ArmClient

HERE = FilePath(__file__).resolve().parent
LAYOUT_FILE = HERE / "layout.yaml"
sys.path.insert(0, str(HERE.parent / "software"))  # Om's pour controller lives in software/laminar
ROLL_MARGIN_DEG = 2.0  # stay this far inside the wrist-roll limit


def load_layout(path: FilePath = LAYOUT_FILE) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def route(layout: dict, vial: str, compartment: str) -> tuple[float, float]:
    """(dx, dy) in mm from vial to compartment. Clear errors for typos and unmeasured routes."""
    routes = layout.get("routes") or {}
    if vial not in routes:
        raise KeyError(f"unknown vial {vial!r}; layout.yaml has {', '.join(routes)}")
    if compartment not in (routes[vial] or {}):
        raise KeyError(f"unknown compartment {compartment!r} for {vial}; known: {', '.join(routes[vial])}")
    d = routes[vial][compartment]
    if d is None:
        raise ValueError(f"routes.{vial}.{compartment} is not measured yet (null in layout.yaml)")
    return float(d[0]), float(d[1])


def roll_room_deg(base_deg: float, direction: int, joint: int = 6) -> float:
    """How far the pour roll can turn from base_deg in `direction` before its soft limit."""
    lo, hi = M.JOINT_LIMITS_DEG[joint]
    room = (hi - base_deg) if direction > 0 else (base_deg - lo)
    return max(0.0, room - ROLL_MARGIN_DEG)


class ServerPourArm:
    """Adapter so software/laminar/pour.run_pour can drive the real arm: only stream_q is used, and only the
    pour joint's offset from base_q matters (run_pour is called with base_q = zeros, so q[5] IS the tilt)."""

    def __init__(self, client: ArmClient, direction: int):
        self.client = client
        self.direction = direction

    def stream_q(self, q_list, dt: float) -> None:
        for q in q_list:
            self.client.set_tilt(self.direction * math.degrees(float(q[5])))
            time.sleep(dt)


def pour_camera(client: ArmClient, cfg: dict, ml: float, liquid: str) -> str:
    from laminar.pour import RECIPES, PourController, PourRecipe, UdpPourFeed, run_pour

    direction = int(cfg.get("direction", 1))
    base = client.start_tilt_stream(int(cfg.get("joint", 6)), float(cfg.get("max_rate_deg_s", 120.0)))
    room = roll_room_deg(base, direction, int(cfg.get("joint", 6))) if not client.dry_run else 90.0
    recipe = RECIPES.get(liquid, PourRecipe())
    recipe = dataclasses.replace(recipe, max_tilt=min(recipe.max_tilt, math.radians(room)))
    feed = UdpPourFeed(int(cfg.get("udp_port", 5555)))
    try:
        ctrl = PourController(ml, liquid, recipe)
        run_pour(ServerPourArm(client, direction), ctrl, feed.latest, [0.0] * 6, joint=5)
    finally:
        feed.close()
        client.end_tilt_stream()
    return f"pour {ctrl.state.value}: {ctrl.reason}"


def pour_fixed(client: ArmClient, cfg: dict, tilt_deg: float, hold_s: float) -> str:
    direction = int(cfg.get("direction", 1))
    base = client.start_tilt_stream(int(cfg.get("joint", 6)), float(cfg.get("max_rate_deg_s", 120.0)))
    tilt = min(abs(tilt_deg), roll_room_deg(base, direction) if not client.dry_run else abs(tilt_deg))
    try:
        for phase, (a, b, secs) in enumerate(((0.0, tilt, 1.5), (tilt, tilt, hold_s), (tilt, 0.0, 1.5))):
            n = max(1, int(secs * 30))
            for k in range(1, n + 1):
                client.set_tilt(direction * (a + (b - a) * k / n))
                time.sleep(1 / 30)
    finally:
        client.end_tilt_stream()
    return f"fixed pour: {tilt:.0f} deg for {hold_s:.1f} s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vial")
    ap.add_argument("compartment")
    ap.add_argument("--run", action="store_true", help="move the arm (otherwise plan and print only)")
    ap.add_argument("--pour", choices=["camera", "fixed", "none"], default="camera")
    ap.add_argument("--ml", type=float, default=2.0, help="target volume for the camera pour")
    ap.add_argument("--liquid", default="blue", help="expected liquid colour for the camera pour")
    ap.add_argument("--tilt", type=float, default=80.0, help="fixed pour: roll angle, degrees")
    ap.add_argument("--hold", type=float, default=3.0, help="fixed pour: seconds at full tilt")
    a = ap.parse_args()

    layout = load_layout()
    try:
        dxy = route(layout, a.vial, a.compartment)
    except (KeyError, ValueError) as e:
        sys.exit(f"NOT MOVING: {e}")
    lift_mm, lower_mm = float(layout.get("lift_mm", 100)), float(layout.get("lower_mm", 60))

    client = ArmClient(dry_run=not a.run)
    try:
        q0 = client.current_joints()
    except RuntimeError as e:
        sys.exit(str(e))
    try:
        out = M.plan_transfer(q0, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
        back = M.plan_return(out[-1].final_joints, dxy, lift_mm=lift_mm, lower_mm=lower_mm)
    except M.PlanError as e:
        sys.exit(f"NOT MOVING: {e}")

    print(f"{a.vial} -> {a.compartment}: offset ({dxy[0]:+.0f}, {dxy[1]:+.0f}) mm")
    for p in out + back:
        print("  " + p.summary())
    total = sum(p.duration_s for p in out + back)
    print(f"  moves total {total:.1f} s (+ pour)")
    if not a.run:
        print("Plan only. Add --run to move the arm.")
        return

    try:
        for p in out:
            client.run(p)
        if a.pour == "camera":
            print(pour_camera(client, layout.get("pour") or {}, a.ml, a.liquid))
        elif a.pour == "fixed":
            print(pour_fixed(client, layout.get("pour") or {}, a.tilt, a.hold))
        for p in back:
            client.run(p)
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"STOPPED (arm is holding where it is): {e}")
    print("transfer done: tube back in its slot")


if __name__ == "__main__":
    main()
