"""Teach A and B during a teleop recording, then replay: the recorded rest -> A, then planned A -> B.

    1. python teleop.py --record --camera -1          (leave running)
    2. move the leader to A:  python episode.py mark A
       move the leader to B:  python episode.py mark B
       (each reads the follower's commanded joints from the newest row of the live data.csv)
    3. stop teleop (q, or create robotics/STOP), start arm_server.py
    4. python episode.py plan                          # prints the moves, saves 2-D coordinates
       python episode.py replay --run                  # moves the arm

Replay = the recorded rest -> A segment of the episode, gripper included (leading idle frames skipped,
slowed if any joint would exceed arm_server's path speed limit), then
lift 150 mm -> travel in that locked horizontal plane to above B -> lower onto B's height.
The travel keeps A's uprightness and takes only B's turn about vertical (hand-taught poses are never
exactly upright, and the planner refuses tilt changes over 3 deg).

episode.json (next to data.csv) holds A, B (frame, t, commanded + measured joints, xyz in mm) and
travel {dxy_mm, dz_mm}. Angles in degrees from the follower zero pose; xyz in the arm base frame
(x away from the base, y left, z up).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path as FilePath

import numpy as np

import motion as M
from arm_client import ArmClient

HERE = FilePath(__file__).resolve().parent
RECORDINGS = HERE / "recordings"
LIFT_MM = 150.0
MIN_LOWER_MM = 110.0  # the carry runs at least this far above B, so the last move is always a real lower
REPLAY_MAX_DEG_S = 80.0  # arm_server refuses path steps over 90 deg/s
IDLE_DEG = 0.5           # leading frames within this of frame 0 are skipped


def latest_recording() -> FilePath:
    runs = sorted(p for p in RECORDINGS.glob("*/data.csv"))
    if not runs:
        sys.exit("no recordings/*/data.csv: run teleop.py --record first")
    return runs[-1].parent


def read_rows(rec: FilePath) -> list[dict]:
    with open(rec / "data.csv", newline="") as f:
        return list(csv.DictReader(f))


def joints(row: dict, kind: str = "command") -> dict[int, float]:
    return {m: float(row[f"{kind}.J{m}"]) for m in range(1, 8)}


def arm(q: dict) -> dict[int, float]:
    return {int(m): float(v) for m, v in q.items() if 1 <= int(m) <= 6}


def load_episode(rec: FilePath) -> dict:
    f = rec / "episode.json"
    return json.loads(f.read_text()) if f.exists() else {}


def mark(rec: FilePath, name: str) -> None:
    row = read_rows(rec)[-1]
    ep = load_episode(rec)
    q = joints(row)
    p, _ = M.fk_mm(arm(q))
    ep[name] = {"frame": int(row["frame"]), "t": float(row["t"]), "command": q,
                "state": joints(row, "state"), "xyz_mm": [round(float(v), 1) for v in p]}
    (rec / "episode.json").write_text(json.dumps(ep, indent=2))
    print(f"{name}: frame {row['frame']} (t={row['t']} s)  "
          + "  ".join(f"J{m}={v:6.1f}" for m, v in q.items())
          + f"  ->  x={p[0]:.0f} y={p[1]:.0f} z={p[2]:.0f} mm")


def plan_a_to_b(ep: dict) -> list[M.Path]:
    """lift at A -> flat travel to above B -> lower to B's height. The lift is LIFT_MM, or more when B sits higher
    than A, so the carry plane is always at least MIN_LOWER_MM above B."""
    qa, qb = arm(ep["A"]["command"]), arm(ep["B"]["command"])
    pa, Ra = M.fk_mm(qa)
    pb, Rb = M.fk_mm(qb)
    Rd = Rb @ Ra.T  # keep A's uprightness, take only B's turn about vertical
    yaw = math.atan2(Rd[1, 0] - Rd[0, 1], Rd[0, 0] + Rd[1, 1])
    c, s = math.cos(yaw), math.sin(yaw)
    R1 = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]) @ Ra
    lift_mm = max(LIFT_MM, (pb[2] - pa[2]) + MIN_LOWER_MM)
    lift = M.plan_lift(qa, lift_mm)
    travel = M.plan_travel(lift.final_joints, {"xy_mm": pb[:2].tolist(), "R": R1.tolist()}, label="travel A -> B")
    lower = M.plan_lower(travel.final_joints, lift_mm - (pb[2] - pa[2]))
    return [lift, travel, lower]


def recorded_segment(rec: FilePath, ep: dict, fps: float) -> tuple[list[dict[int, float]], float]:
    """Commanded J1-J7 (gripper included) from the first frame that moves up to A, and a replay rate
    within the arm's path speed limit."""
    rows = read_rows(rec)[: ep["A"]["frame"] + 1]
    pts = [joints(r) for r in rows]
    first = next((i for i, q in enumerate(pts) if max(abs(q[m] - pts[0][m]) for m in q) > IDLE_DEG), len(pts) - 1)
    pts = pts[max(0, first - 1):]
    worst = max((max(abs(b[m] - a[m]) for m in range(1, 7)) for a, b in zip(pts, pts[1:])), default=0.0)
    hz = fps if worst * fps <= REPLAY_MAX_DEG_S else REPLAY_MAX_DEG_S / worst
    return pts, hz


def reverse_path(p: M.Path) -> M.Path:
    """The same move played backwards: same joint targets in reverse order, same timing."""
    return M.Path(times_s=list(p.times_s), joints=list(p.joints[::-1]), label=f"back: {p.label}",
                  start_mm=p.end_mm, end_mm=p.start_mm, max_pos_err_mm=p.max_pos_err_mm,
                  max_ang_err_deg=p.max_ang_err_deg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["mark", "plan", "replay"])
    ap.add_argument("name", nargs="?", choices=["A", "B"], help="mark: which pose")
    ap.add_argument("--rec", default=None, help="recording folder (default: newest in recordings/)")
    ap.add_argument("--run", action="store_true", help="replay: move the arm (otherwise print only)")
    ap.add_argument("--no-return", action="store_true",
                    help="replay: stop at B (default: play everything back in reverse so the tube returns to A)")
    a = ap.parse_args()
    rec = FilePath(a.rec) if a.rec else latest_recording()

    if a.command == "mark":
        if not a.name:
            ap.error("mark needs A or B")
        mark(rec, a.name)
        return

    ep = load_episode(rec)
    if "A" not in ep or "B" not in ep:
        sys.exit(f"{rec.name}: mark A and B first")
    pa, pb = np.array(ep["A"]["xyz_mm"]), np.array(ep["B"]["xyz_mm"])
    ep["travel"] = {"dxy_mm": [round(float(v), 1) for v in (pb - pa)[:2]], "dz_mm": round(float(pb[2] - pa[2]), 1)}
    (rec / "episode.json").write_text(json.dumps(ep, indent=2))
    try:
        moves = plan_a_to_b(ep)
    except M.PlanError as e:
        sys.exit(f"NOT MOVING: {e}")
    fps = json.loads((rec / "meta.json").read_text()).get("fps", 20.0)
    seg, hz = recorded_segment(rec, ep, fps)
    print(f"{rec.name}: A {pa.round().tolist()} -> B {pb.round().tolist()} mm, "
          f"2-D travel (dx, dy) = {ep['travel']['dxy_mm']} mm, dz = {ep['travel']['dz_mm']} mm")
    print(f"  replay rest -> A: {len(seg)} recorded frames at {hz:.1f} Hz ({(len(seg) - 1) / hz:.1f} s)")
    for p in moves:
        print("  " + p.summary())
    if a.command == "plan" or not a.run:
        print("Plan only. Use: python episode.py replay --run")
        return

    client = ArmClient()
    try:
        client.current_joints()
        print("to the first recorded frame")
        client._send({"op": "move", "joints": {str(m): v for m, v in arm(seg[0]).items()}, "speed": 15.0})
        client._send({"op": "gripper", "target": seg[0][7], "speed": 30.0})
        print("replaying rest -> A (arm + gripper)")
        st = client._send({"op": "path", "points": [{str(m): round(v, 3) for m, v in q.items()} for q in seg],
                           "hz": hz})
        print("  " + str(st.get("result")))
        for p in moves:
            st = client.run(p)
            print("  " + str(st.get("result")))
        if a.no_return:
            print("replay done: at B")
            return
        time.sleep(1.0)
        # back the way it came: every planned move reversed along the same joint path (lift at B, travel back
        # above A, lower onto A), then the recording played backwards, gripper included, so the jaws open at A
        # (releasing the tube where it was taken from) before the arm retraces the teleop path to rest
        for p in reversed(moves):
            st = client.run(reverse_path(p))
            print("  " + str(st.get("result")))
        print("replaying A -> rest backwards (gripper releases first)")
        st = client._send({"op": "path", "points": [{str(m): round(v, 3) for m, v in q.items()} for q in seg[::-1]],
                           "hz": hz})
        print("  " + str(st.get("result")))
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"STOPPED (arm is holding where it is): {e}")
    print("replay done: tube back at A, arm back where the recording started")


if __name__ == "__main__":
    main()
