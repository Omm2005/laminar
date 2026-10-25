"""Carry a vial to a container found by its marker, and back: python deliver.py A2c [--run]

"A2c" = vial A -> container c (vials A-D: robotics/paths/vial_A..D, recorded by teleop; containers a-d = 1-4, the
tray markers 10-13, found by markers.py). The flow:
    1. find the tray: scan pose, lock in on the markers (markers.py), container c's (x, y)
    2. grab: back to the neutral (rest) pose, replay the teleop grab of vial A (gripper closes on it)
    3. carry: rise, travel flat, lower over container c (episode.plan_a_to_b: the same moves as grab.py transfer),
       with the transfer heights (motion.TRANSFER_LIFT_MM up, TRANSFER_LOWER_MM down). After the travel and the lower
       the sag is measured and corrected (markers.settle_on), so the tip really is over the marker centre.
    4. back: rise, travel back over vial A's slot, lower to the grasp pose, replay the grab backwards (the gripper
       releases the vial in its slot, the arm returns to rest).
Nothing moves without --run (arm_server.py running); without it, plan from a scan photo: --image tray_scan_3.jpg.

Pour poses relative to the markers (container_offsets.yaml):
    python deliver.py learn-offsets --run   tray at its ORIGINAL place (where vial_A..D_final_position were taught):
                                            scan it, store each final position in its marker's frame (tip offset,
                                            height, gripper orientation), arm back to rest
Once learned, a job goes to that container's pour pose instead: wherever the tray now is, the gripper ends at the
same spot and orientation relative to the container (lift, travel, lower, then a small in-place turn into the exact
orientation), so a wrist roll (J6) alone pours into it. Without learned offsets: over the marker, CONTAINER_OFFSET_MM.
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

# the same rise and lower as every transfer (motion.py): up 150, down 110, so the tip ends 40 mm above the grasp height
ABOVE_MM = M.TRANSFER_LIFT_MM - M.TRANSFER_LOWER_MM
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


# --- pour poses taught relative to the markers -------------------------------------------------------------------
# vial_<X>_final_position (spots.yaml, taught by teleop with the tray at its original place) is the pose over
# container x from which a pure wrist roll (J6) pours into it. `learn-offsets` scans the tray once in that same place
# and stores each pose in its container's MARKER frame (origin = marker centre, x axis = the tray line, marker 10 ->
# 13): the tip's (dx, dy), its height, and the gripper's full orientation. Wherever the tray goes next, the pose goes
# with it: same spot and same orientation relative to the container, so the same wrist roll still pours into it.
# All taught joints are teleop COMMANDS, and the moves replay commands the same way, so the arm's sag is in both and
# cancels (no settle_on correction on this path).
OFFSETS_FILE = T.HERE / "container_offsets.yaml"
FINAL_SPOT = "vial_{}_final_position"


def learn_offsets(pose: dict) -> dict:
    """Tray pose seen with the tray at its original place -> {container: offset} for every container whose
    vial_<x>_final_position exists. Each offset: marker id, dxy_mm (tray frame), distance_mm and bearing_deg (the same
    in polar form), z_mm (tip height, the table is flat), heading_offset_deg (the gripper's approach heading minus
    the tray heading), R_tray (tool orientation with the tray heading removed) and the joints it came from."""
    from spots import load_spots
    spots = load_spots()
    th = float(pose["heading_deg"])
    ids = {c: i for i, c in T.MARKERS.items()}
    out = {}
    for cont in sorted(pose["containers"]):
        name = FINAL_SPOT.format("ABCD"[cont - 1])
        q = spots.get(name)
        if not q:
            continue
        p, R = M.fk_mm(q)
        d = T.rot(-th) @ (p[:2] - np.asarray(pose["containers"][cont], float))
        approach = R[:, T.APPROACH]
        heading = math.degrees(math.atan2(approach[1], approach[0]))
        out[cont] = {
            "marker": ids[cont], "spot": name,
            "dxy_mm": [round(float(v), 2) for v in d],
            "distance_mm": round(float(np.linalg.norm(d)), 2),
            "bearing_deg": round(math.degrees(math.atan2(d[1], d[0])), 2),
            "z_mm": round(float(p[2]), 2),
            "heading_offset_deg": round((heading - th + 180.0) % 360.0 - 180.0, 2),
            "R_tray": [[round(float(v), 6) for v in row] for row in T.yaw(-th) @ R],
            "joints": {j: round(float(q[j]), 2) for j in range(1, 7)},
        }
    return out


def save_offsets(offsets: dict, pose: dict, path=OFFSETS_FILE) -> None:
    head = ("# Pour poses relative to each container's ArUco marker, learned by `python deliver.py learn-offsets`.\n"
            "# Tray frame: origin = the marker centre, x = along the tray line (marker 10 -> 13), mm and degrees.\n")
    data = {"learned": T.time.strftime("%Y-%m-%d %H:%M:%S"),
            "tray_at_learn": {"xy_mm": [round(float(v), 2) for v in pose["xy"]],
                              "heading_deg": round(float(pose["heading_deg"]), 2),
                              "rms_mm": round(float(pose["rms_mm"]), 2), "seen": [int(i) for i in pose["seen"]]},
            "containers": offsets}
    path.write_text(head + yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=120))


def load_offsets(path=OFFSETS_FILE) -> dict:
    """{container: offset} from container_offsets.yaml, {} when nothing was learned yet."""
    if not path.exists():
        return {}
    return {int(k): v for k, v in (yaml.safe_load(path.read_text()) or {}).get("containers", {}).items()}


def pour_pose(pose: dict, cont: int, offsets: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    """Where the gripper goes over container `cont` for the tray seen now: (tip mm, orientation 3x3, IK seed joints).
    The learned offset turned and moved with the tray; the height is the learned one."""
    off = offsets[cont]
    th = float(pose["heading_deg"])
    xy = np.asarray(pose["containers"][cont], float) + T.rot(th) @ np.asarray(off["dxy_mm"], float)
    p = np.array([xy[0], xy[1], float(off["z_mm"])])
    R = T.yaw(th) @ np.asarray(off["R_tray"], float)
    q0 = {int(j): float(v) for j, v in off["joints"].items()}
    p0, _ = M.fk_mm(q0)
    turn = math.degrees(math.atan2(p[1], p[0]) - math.atan2(p0[1], p0[0]))
    return p, R, {**q0, 1: q0[1] + turn}


MAX_HEADING_GIVE_DEG = 20.0  # the taught pour poses sit at J4's limit (89 of 90 deg): when the tray's new place
                             # makes the exact orientation unreachable, turn the gripper about vertical, this far at most


def reachable_pour(p, R, seed) -> tuple[np.ndarray, dict]:
    """(orientation, joints) for the pour pose at tip p: R itself when the arm can reach it, else R turned about the
    vertical by the smallest angle (2.5 deg steps, MAX_HEADING_GIVE_DEG at most) that it can. The vial stays exactly
    as upright as taught; only the pour direction turns a little. Raises PlanError if nothing reaches."""
    steps = np.arange(2.5, MAX_HEADING_GIVE_DEG + 1e-9, 2.5)
    worst = None
    for give in [0.0, *[s * sign for s in steps for sign in (1, -1)]]:
        Rg = T.yaw(give) @ np.asarray(R, float)
        q, pe, ae = M.ik({**seed, 1: seed[1] + give}, p, Rg)
        if pe <= M.POS_TOL_MM and ae <= M.ANG_TOL_DEG:
            if give:
                print(f"  pour pose: exact orientation out of reach here, gripper turned {give:+.1f} deg about vertical")
            return Rg, dict(q)
        worst = worst or (pe, ae)
    raise M.PlanError(f"can't reach the pour pose at ({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}) mm, even turned "
                      f"+-{MAX_HEADING_GIVE_DEG:g} deg (IK off by {worst[0]:.1f} mm / {worst[1]:.1f} deg)")


def plan_pour_carry(vial: str, p, R, seed) -> tuple[list, dict, dict]:
    """[lift, travel, lower, turn] from the vial's grasp pose to the pour pose (p, R), the grasp joints and the pour
    joints. Lift and travel keep the vial as upright as it was grabbed (travel turns it only about vertical, to the
    pour pose's heading); after the lower, `turn` blends in place into the exact pour orientation (a few degrees)."""
    qa = {j: v for j, v in load(vial)["initial_position"].items() if j <= 6}
    R, qb = reachable_pour(p, R, seed)
    pa, Ra = M.fk_mm(qa)
    Rd = R @ Ra.T  # the grasp orientation turned about vertical to the pour pose's heading
    yw = math.degrees(math.atan2(Rd[1, 0] - Rd[0, 1], Rd[0, 0] + Rd[1, 1]))
    lift_mm = max(M.TRANSFER_LIFT_MM, (p[2] - pa[2]) + M.TRANSFER_LOWER_MM)
    lift = M.plan_lift(qa, lift_mm)
    travel = M.plan_travel(lift.final_joints, {"xy_mm": p[:2].tolist(), "R": (T.yaw(yw) @ Ra).tolist()},
                           label="travel to the pour pose")
    lower = M.plan_lower(travel.final_joints, lift_mm - (p[2] - pa[2]))
    turn = M.plan_line(lower.final_joints, p, R, speed_mm_s=M.DEFAULT_LOWER_SPEED, label="turn into the pour pose")
    return [lift, travel, lower, turn], qa, dict(qb)


def plan_carry(vial: str, xy) -> tuple[list, dict]:
    """[lift, travel, lower] from the vial's grasp pose to over (x, y), and the grasp joints. The vial stays as
    upright as it was grabbed; only its heading turns, with the base."""
    qa = {j: v for j, v in load(vial)["initial_position"].items() if j <= 6}
    pa, Ra = M.fk_mm(qa)
    target = np.array([xy[0], xy[1], pa[2] + ABOVE_MM])
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
    ap.add_argument("job", help="vial 2 container, e.g. A2c; or learn-offsets (tray at its original place)")
    ap.add_argument("--run", action="store_true", help="move the arm (arm_server.py must be running)")
    ap.add_argument("--image", help="without --run: find the tray in this scan-pose photo")
    ap.add_argument("--dj1", type=float, default=0.0, help="with --image: the base was turned this much (deg)")
    ap.add_argument("--camera", type=int, default=T.CAMERA)
    a = ap.parse_args()
    learning = a.job == "learn-offsets"
    vial, cont = (None, None) if learning else parse(a.job)
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

    if learning:
        print(f"tray at ({pose['xy'][0]:.1f}, {pose['xy'][1]:.1f}) mm, heading {pose['heading_deg']:.1f} deg, "
              f"fit {pose['rms_mm']:.1f} mm from markers {pose['seen']}")
        offsets = learn_offsets(pose)
        for c, o in offsets.items():
            m = pose["containers"][c]
            print(f"  container {'abcd'[c - 1]} (marker {o['marker']} at ({m[0]:.1f}, {m[1]:.1f})): "
                  f"tip {o['distance_mm']:.1f} mm from the marker at {o['bearing_deg']:+.1f} deg "
                  f"(dx {o['dxy_mm'][0]:+.1f}, dy {o['dxy_mm'][1]:+.1f}), z {o['z_mm']:.1f} mm, "
                  f"gripper heading {o['heading_offset_deg']:+.1f} deg from the tray line")
        save_offsets(offsets, pose)
        print(f"saved {OFFSETS_FILE.name}")
        if client:
            print(str(client._send({"op": "rest"}).get("result")))
        return

    offsets = load_offsets()
    if cont in offsets:
        p, R, seed = pour_pose(pose, cont, offsets)
        print(f"{vial} -> container {'abcd'[cont - 1]} ({cont}): pour pose ({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}) mm, "
              f"tray heading {pose['heading_deg']:.1f} deg, seen from markers {pose['seen']}")
        try:
            moves, q_grasp, q_pour = plan_pour_carry(vial, p, R, seed)
        except M.PlanError as e:
            raise SystemExit(f"NOT MOVING: {e}") from None
        for mv in moves:
            print("  " + mv.summary())
        print("  pour joints " + "  ".join(f"J{j}={q_pour[j]:.1f}" for j in range(1, 7)))
        print("  then the same moves backwards, the vial released in its slot, arm to rest")
        if not client:
            print("print only: add --run to move")
            return
        from episode import reverse_path
        try:
            grab.play(vial, run=True)
            for mv in moves:
                print("  " + str(client.run(mv).get("result")))
            print(f"at the pour pose over container {'abcd'[cont - 1]}: holding 2 s")
            T.time.sleep(2.0)
            for mv in reversed(moves):
                print("  " + str(client.run(reverse_path(mv)).get("result")))
            grab.play(vial, run=True, reverse=True)
        except (RuntimeError, TimeoutError, M.PlanError) as e:
            sys.exit(f"STOPPED (arm_server holds the arm where it is): {e}")
        print(f"{a.job} done: vial back in its slot, arm at rest")
        return

    xy = container_target(pose, cont)
    print(f"{vial} -> container {'abcd'[cont - 1]} ({cont}): marker at ({xy[0]:.1f}, {xy[1]:.1f}) mm, "
          f"tray seen from markers {pose['seen']}")

    try:
        moves, q_grasp = plan_carry(vial, xy)
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
