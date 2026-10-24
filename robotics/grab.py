"""Save and replay labelled teleop motions (robotics/paths/<name>.py), e.g. rest -> vial A.

    # during / after a teleop recording: cut rest -> the pose at a frame into paths/vial_B.py, and save that
    # pose's joints 1-6 as vial_B_initial_position in spots.yaml
    python grab.py save vial_B --rec recordings/<run> [--frame N] [--label "..."]
    python grab.py list
    python grab.py play vial_A            # print what it would do
    python grab.py play vial_A --run      # rest -> vial A (gripper closes on it), arm_server.py running
    python grab.py play vial_A --reverse --run   # back: gripper releases first, then vial A -> rest

A motion starts from the first frame where the arm moves (still frames at the start are dropped) and ends at
the chosen frame (default: the newest). It is replayed through arm_server's `path` op at the recording rate,
slowed down if any joint step would exceed the server's speed limit. Units: degrees, follower frame.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path as FilePath

from episode import IDLE_DEG, REPLAY_MAX_DEG_S, joints, read_rows
from paths import HERE as PATHS_DIR
from paths import load, names
from spots import save_spot

HERE = FilePath(__file__).resolve().parent


def cut_motion(rec: FilePath, frame: int | None = None, fps: float = 20.0) -> tuple[list[dict[int, float]], float, int]:
    """(points {1..7: deg}, replay hz, end frame) for rest -> the pose at `frame` (default: last row).
    Still frames at the start are dropped; the rate is slowed if a step would exceed REPLAY_MAX_DEG_S."""
    rows = read_rows(rec)
    end = len(rows) - 1 if frame is None else int(frame)
    if not 0 <= end < len(rows):
        raise SystemExit(f"frame {end} is outside the recording (0..{len(rows) - 1})")
    pts = [joints(r) for r in rows[: end + 1]]
    first = next((i for i, q in enumerate(pts) if max(abs(q[m] - pts[0][m]) for m in q) > IDLE_DEG), len(pts) - 1)
    pts = pts[max(0, first - 1):]
    worst = max((max(abs(b[m] - a[m]) for m in range(1, 7)) for a, b in zip(pts, pts[1:])), default=0.0)
    hz = fps if worst * fps <= REPLAY_MAX_DEG_S else REPLAY_MAX_DEG_S / worst
    return pts, hz, end


def write_motion(name: str, label: str, source: str, hz: float, pts: list[dict[int, float]]) -> FilePath:
    """paths/<name>.py with the motion as plain Python data (readable diffs, importable)."""
    end = pts[-1]
    rows = ",\n".join("    [" + ", ".join(f"{p[m]:.2f}" for m in range(1, 8)) + "]" for p in pts)
    text = (f'"""{label}\n\nRecorded by teleop, cut by `python grab.py save {name}`. Replay: python grab.py play {name} --run\n'
            f'Degrees, follower frame (arm_server.py); columns J1..J6 + gripper (J7)."""\n\n'
            f"NAME = {name!r}\nLABEL = {label!r}\nSOURCE = {source!r}\nHZ = {hz:.3f}\n"
            f"INITIAL_POSITION = {{{', '.join(f'{m}: {end[m]:.2f}' for m in range(1, 8))}}}\n"
            f"POINTS = [\n{rows},\n]\n")
    path = PATHS_DIR / f"{name}.py"
    path.write_text(text)
    return path


def play(name: str, run: bool, reverse: bool = False) -> None:
    """Replay a saved motion through arm_server.py: move to its start (or end, for --reverse), set the gripper,
    then the whole path continuously. Without run: print only."""
    m = load(name)
    pts = m["points"][::-1] if reverse else m["points"]
    secs = (len(pts) - 1) / m["hz"]
    print(f"{name}: {m['label']} | {len(pts)} points, {secs:.1f} s{' (reversed)' if reverse else ''}")
    if not run:
        print("print only: add --run (arm_server.py must be running)")
        return
    from arm_client import ArmClient
    client = ArmClient()
    try:
        client.current_joints()
        first = pts[0]
        client._send({"op": "move", "joints": {str(j): first[j] for j in range(1, 7)}, "speed": 15.0})
        client._send({"op": "gripper", "target": first[7], "speed": 30.0})
        st = client._send({"op": "path", "points": [{str(j): round(p[j], 3) for j in range(1, 8)} for p in pts],
                           "hz": m["hz"]})
        print("  " + str(st.get("result")))
    except (RuntimeError, TimeoutError) as e:
        sys.exit(f"STOPPED (arm is holding where it is): {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["save", "play", "list"])
    ap.add_argument("name", nargs="?", help="motion name, e.g. vial_A")
    ap.add_argument("--rec", help="save: recording folder")
    ap.add_argument("--frame", type=int, default=None, help="save: last frame of the motion (default: newest)")
    ap.add_argument("--label", default=None, help="save: what the motion does")
    ap.add_argument("--spot", default=None, help="save: spots.yaml name (default <name>_initial_position)")
    ap.add_argument("--run", action="store_true", help="play: move the arm")
    ap.add_argument("--reverse", action="store_true", help="play: backwards (gripper releases first)")
    a = ap.parse_args()
    if a.op == "list":
        for n in names():
            m = load(n)
            print(f"  {n:12s} {len(m['points']):5d} pts {(len(m['points']) - 1) / m['hz']:5.1f} s  {m['label']}")
        return
    if not a.name:
        ap.error("needs a motion name")
    if a.op == "play":
        play(a.name, a.run, a.reverse)
        return
    if not a.rec:
        ap.error("save needs --rec <recording folder>")
    rec = FilePath(a.rec)
    pts, hz, end = cut_motion(rec, a.frame)
    label = a.label or f"rest -> {a.name.replace('_', ' ')} initial position (gripper closes on it)"
    path = write_motion(a.name, label, f"{rec.name} frames ..{end}", hz, pts)
    spot = a.spot or f"{a.name}_initial_position"
    save_spot(spot, {j: pts[-1][j] for j in range(1, 7)})
    q = pts[-1]
    print(f"saved {path.relative_to(HERE)}: {len(pts)} points, {(len(pts) - 1) / hz:.1f} s at {hz:.1f} Hz")
    print(f"saved {spot} = " + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 8)))


if __name__ == "__main__":
    main()
