"""Carry a vial to a container found by its marker, and back: python deliver.py A2c [--run]

"A2c" = vial A -> container c (vials A-D: robotics/paths/vial_A..D, recorded by teleop; containers a-d = 1-4, the
tray markers 10-13, found by markers.py). The flow:
    1. find the tray: scan pose, lock in on the markers (markers.py), container c's (x, y)
    2. grab: back to the neutral (rest) pose, replay the teleop grab of vial A (gripper closes on it)
    3. carry: rise, travel flat, lower over container c (episode.plan_a_to_b: the same moves as grab.py transfer),
       ending with the tip ABOVE_MM above the height it grabbed the vial at. After the travel and after the lower
       the sag is measured and corrected (markers.settle_on), so the tip really is over the marker centre.
    4. back: rise, travel back over vial A's slot, lower to the grasp pose, replay the grab backwards (the gripper
       releases the vial in its slot, the arm returns to rest).
Nothing moves without --run (arm_server.py running); without it, plan from a scan photo: --image tray_scan_3.jpg.
Positions relative to the markers (where to actually pour) come later: CONTAINER_OFFSET_MM.
"""
from __future__ import annotations

import argparse
import math
import re
import sys

import cv2
import numpy as np
import yaml

import markers as T
import motion as M
from episode import plan_a_to_b
from paths import load

ABOVE_MM = 30.0  # over the container the tip stops this much higher than where it grabbed the vial
# where to go relative to each container's marker, in the tray frame (x along marker 10 -> 13), mm. All zero for
# now: straight over the marker centre.
CONTAINER_OFFSET_MM = {1: (0.0, 0.0), 2: (0.0, 0.0), 3: (0.0, 0.0), 4: (0.0, 0.0)}


def parse(job: str) -> tuple[str, int]:
    """"A2c" -> ("vial_A", 3)."""
    m = re.fullmatch(r"([A-Da-d])2([A-Da-d])", job.strip())
    if not m:
        raise SystemExit(f"{job!r}: expected vial letter, 2, container letter, e.g. A2c")
    return f"vial_{m.group(1).upper()}", "abcd".index(m.group(2).lower()) + 1


def container_target(pose: dict, cont: int) -> np.ndarray:
    """(x, y) mm to go to for container `cont`: its marker centre plus that container's offset, turned with the tray."""
    return pose["containers"][cont] + T.rot(pose["heading_deg"]) @ np.asarray(CONTAINER_OFFSET_MM[cont], float)


def plan_carry(vial: str, xy, above_mm: float = ABOVE_MM) -> tuple[list, dict]:
    """[lift, travel, lower] from the vial's grasp pose to over (x, y), and the grasp joints. The vial stays as
    upright as it was grabbed; only its heading turns, with the base."""
    qa = {j: v for j, v in load(vial)["initial_position"].items() if j <= 6}
    pa, Ra = M.fk_mm(qa)
    target = np.array([xy[0], xy[1], pa[2] + above_mm])
    turn = math.degrees(math.atan2(xy[1], xy[0]) - math.atan2(pa[1], pa[0]))
    seed = {**qa, 1: qa[1] + turn}
    qb, pe, ae = M.ik(seed, target, T.yaw(turn) @ Ra)
    if pe > M.POS_TOL_MM or ae > M.ANG_TOL_DEG:
        raise M.PlanError(f"can't reach ({xy[0]:.0f}, {xy[1]:.0f}) holding the vial upright "
                          f"(IK off by {pe:.1f} mm / {ae:.1f} deg)")
    ep = {"A": {"command": {str(j): v for j, v in qa.items()}}, "B": {"command": {str(j): float(v) for j, v in
                                                                                 dict(qb).items()}}}
    return plan_a_to_b(ep), qa


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", help="vial 2 container, e.g. A2c")
    ap.add_argument("--run", action="store_true", help="move the arm (arm_server.py must be running)")
    ap.add_argument("--image", help="without --run: find the tray in this scan-pose photo")
    ap.add_argument("--dj1", type=float, default=0.0, help="with --image: the base was turned this much (deg)")
    ap.add_argument("--above", type=float, default=ABOVE_MM, help="tip over the container, mm above the grasp height")
    ap.add_argument("--camera", type=int, default=T.CAMERA)
    a = ap.parse_args()
    vial, cont = parse(a.job)
    calib = T.load_calib()

    # 1. find the tray
    client = None
    if a.run:
        from arm_client import ArmClient
        import grab
        client = ArmClient()
        pose, _ = T.locate_tray(client, calib, a.camera, confirm=lambda *x: None)
    else:
        if not a.image:
            raise SystemExit("without --run, give a scan-pose photo: --image tray_scan_3.jpg [--dj1 deg]")
        pose = T.find_tray(cv2.imread(a.image), calib, a.dj1)
    xy = container_target(pose, cont)
    print(f"{vial} -> container {'abcd'[cont - 1]} ({cont}): marker at ({xy[0]:.1f}, {xy[1]:.1f}) mm, "
          f"tray seen from markers {pose['seen']}")

    try:
        moves, q_grasp = plan_carry(vial, xy, a.above)
    except M.PlanError as e:
        raise SystemExit(f"NOT MOVING: {e}") from None
    for p in moves:
        print("  " + p.summary())
    print("  then back the same way, the vial released in its slot, arm to rest")
    if not client:
        print("print only: add --run to move")
        return

    try:
        # 2. grab: play() moves to the recording's start (the rest pose) and replays the teleop grab
        grab.play(vial, run=True)
        # 3. carry, correcting the sag over the container
        lift, travel, lower = moves
        print("  " + str(client.run(lift).get("result")))
        print("  " + str(client.run(travel).get("result")))
        carry_t, R_t = M.fk_mm(travel.final_joints)
        hover_t, _ = M.fk_mm(lower.final_joints)
        _, learned = T.settle_on(client, carry_t)
        client.run(M.plan_line(client.current_joints(), hover_t + learned, None, speed_mm_s=M.DEFAULT_LOWER_SPEED,
                               label="lower over the container"))
        _, learned = T.settle_on(client, hover_t)
        print(f"over container {'abcd'[cont - 1]}: holding 2 s")
        T.time.sleep(2.0)
        # 4. back: up, over the vial's slot (planned, uncorrected: the grab was recorded the same way), down
        q = client.current_joints()
        p, _ = M.fk_mm(q)
        lift_back = M.plan_line(q, [p[0], p[1], carry_t[2] + learned[2]], None, speed_mm_s=M.DEFAULT_LIFT_SPEED,
                                label="lift")
        print("  " + str(client.run(lift_back).get("result")))
        above_slot, R_a = M.fk_mm(lift.final_joints)
        print("  " + str(client.run(M.plan_line(client.current_joints(), above_slot, R_a,
                                                 speed_mm_s=M.DEFAULT_TRAVEL_SPEED, label="travel back")).get("result")))
        slot, _ = M.fk_mm(q_grasp)
        print("  " + str(client.run(M.plan_line(client.current_joints(), slot, R_a, speed_mm_s=M.DEFAULT_LOWER_SPEED,
                                                 label="lower into the slot")).get("result")))
        grab.play(vial, run=True, reverse=True)  # releases the vial, back to rest
    except (RuntimeError, TimeoutError, M.PlanError) as e:
        sys.exit(f"STOPPED (arm_server holds the arm where it is): {e}")
    print(f"{a.job} done: vial back in its slot, arm at rest")


if __name__ == "__main__":
    main()
