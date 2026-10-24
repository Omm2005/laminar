"""Jog the arm by keyboard to find joint poses, and save them into pour/data/poses.yaml.

Starts arm_server if its status is stale. Each nudge is a small arm_server 'move' at --speed.
Comments in poses.yaml are preserved on save (line-based edit, not YAML round-trip).

Keys (Windows console):
  1..6   select joint
  Left / Right arrow      -1 deg / +1 deg on selected joint
  Shift+Left / Shift+Right  -5 deg / +5 deg (also `[` / `]`)
  g      toggle gripper between open and closed targets
  s      prompt for pose name, save current J1..J6 + gripper
  r      reload poses.yaml (just reads to confirm it parses)
  h      print pose list from poses.yaml
  q      send 'rest' to arm_server and exit

Usage:  python pour/jog.py [--speed 5] [--gripper-open 60]
"""
import argparse
import msvcrt
import re
import sys
import time
from pathlib import Path

import yaml

import arm_io

HERE = Path(__file__).resolve().parent
POSES_FILE = HERE / "data" / "poses.yaml"
JOINT_NAMES = {1: "shoulder_pan", 2: "shoulder_lift", 3: "elbow_flex",
               4: "wrist_flex", 5: "wrist_yaw", 6: "wrist_roll"}


def getch():
    """Blocking: returns ('char', raw_char) or ('special', code_int) for arrows / function keys."""
    c = msvcrt.getwch()
    if c in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return ("special", ord(code))
    return ("char", c)


# msvcrt extended codes for arrows (and shifted variants on Windows conhost).
LEFT, RIGHT = 0x4B, 0x4D
SHIFT_LEFT, SHIFT_RIGHT = 0x73, 0x74


def load_pose_names():
    try:
        data = yaml.safe_load(POSES_FILE.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"\npoor poses.yaml ({e}); not loading")
        return []
    return list(data.keys())


def save_pose(name, joints, gripper):
    """Replace the pose stanza named `name` in poses.yaml, preserving inline comments.

    If the pose doesn't exist, append it at the end."""
    text = POSES_FILE.read_text().splitlines()
    header_re = re.compile(rf"^{re.escape(name)}:\s*(#.*)?$")
    val_re = re.compile(r"^(\s+)(J[1-6]|gripper)(:\s*)([^#\n]*?)(\s*)(#.*)?$")
    new_vals = {f"J{m}": f"{joints[m]:.1f}" for m in range(1, 7)}
    new_vals["gripper"] = f"{gripper:.1f}"

    out = []
    i = 0
    found = False
    while i < len(text):
        line = text[i]
        if header_re.match(line):
            found = True
            out.append(line)
            i += 1
            # walk the pose block (indented lines, blank lines inside the block)
            while i < len(text):
                sub = text[i]
                if sub.strip() == "" or sub.startswith(" "):
                    m = val_re.match(sub)
                    if m and m.group(2) in new_vals:
                        indent, key, colon_sp, _old, trail_sp, comment = m.groups()
                        comment = comment or ""
                        sep = (" " if comment and not trail_sp else trail_sp)
                        out.append(f"{indent}{key}{colon_sp}{new_vals[key]}{sep}{comment}".rstrip())
                    else:
                        out.append(sub)
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1
    if not found:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{name}:")
        for m in range(1, 7):
            out.append(f"  J{m}: {joints[m]:.1f}")
        out.append(f"  gripper: {gripper:.1f}")
    POSES_FILE.write_text("\n".join(out) + "\n")


def draw(sel, joints, gripper, msg=""):
    joint_str = "  ".join(
        (f"[J{m}={joints[m]:+7.2f}]" if m == sel else f" J{m}={joints[m]:+7.2f} ")
        for m in range(1, 7)
    )
    sys.stdout.write(f"\r{' ' * 170}\rjog > {joint_str}  grip={gripper:+6.1f}  {msg}")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=5.0, help="joint speed for each nudge, deg/s")
    ap.add_argument("--gripper-open", type=float, default=60.0, help="gripper open target")
    ap.add_argument("--gripper-closed", type=float, default=0.0, help="gripper closed target")
    args = ap.parse_args()

    print("jog > starting arm_server if needed ...")
    proc = arm_io.ensure_server()
    print(f"jog > arm_server {'started by us' if proc else 'already running'}")
    print("jog > keys: 1..6 select joint | <- -> ±1 deg (shift or [ ] for ±5)")
    print("       g toggle gripper | s save pose | r reload yaml | h list poses | q rest and quit")

    st = arm_io.read_status()
    joints = arm_io.current_joints(st)
    gripper = arm_io.current_gripper_target(st)
    sel = 1
    msg = ""

    try:
        while True:
            # redraw with latest actual command values from the server
            st = arm_io.read_status()
            if st:
                joints = arm_io.current_joints(st)
                gripper = arm_io.current_gripper_target(st)
            draw(sel, joints, gripper, msg)
            msg = ""
            # non-blocking wait so the status refreshes while idle
            t0 = time.time()
            while not msvcrt.kbhit() and time.time() - t0 < 0.15:
                time.sleep(0.02)
            if not msvcrt.kbhit():
                continue
            kind, k = getch()
            if kind == "char":
                if k in ("1", "2", "3", "4", "5", "6"):
                    sel = int(k)
                    msg = f"selected J{sel} ({JOINT_NAMES[sel]})"
                elif k in ("q", "Q"):
                    print()
                    print("jog > sending rest ...")
                    try:
                        arm_io.send_cmd("rest", timeout=30.0)
                    except TimeoutError as e:
                        print(f"jog > rest timed out: {e}")
                    return
                elif k in ("g", "G"):
                    new_target = args.gripper_closed if gripper > (args.gripper_open + args.gripper_closed) / 2 else args.gripper_open
                    msg = f"gripper -> {new_target:.1f}"
                    draw(sel, joints, gripper, msg)
                    try:
                        st = arm_io.send_cmd("gripper", target=new_target, speed=30.0)
                        gripper = arm_io.current_gripper_target(st)
                    except TimeoutError as e:
                        msg = f"gripper timeout: {e}"
                elif k == "[":
                    joints[sel] = max(arm_io.LIMITS[sel][0], joints[sel] - 5.0)
                    msg = _nudge(sel, joints, args.speed)
                elif k == "]":
                    joints[sel] = min(arm_io.LIMITS[sel][1], joints[sel] + 5.0)
                    msg = _nudge(sel, joints, args.speed)
                elif k in ("s", "S"):
                    print()
                    name = input("jog > save as (pose name): ").strip()
                    if not name:
                        msg = "save cancelled"
                        continue
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                        msg = f"invalid pose name {name!r}"
                        continue
                    save_pose(name, joints, gripper)
                    msg = f"saved pose '{name}'"
                elif k in ("r", "R"):
                    names = load_pose_names()
                    msg = f"poses.yaml OK ({len(names)} poses)" if names else "poses.yaml empty or unreadable"
                elif k in ("h", "H"):
                    names = load_pose_names()
                    print()
                    print("jog > poses in file: " + (", ".join(names) if names else "<none>"))
                else:
                    msg = f"key {k!r} ignored"
            elif kind == "special":
                if k == LEFT:
                    joints[sel] = max(arm_io.LIMITS[sel][0], joints[sel] - 1.0)
                    msg = _nudge(sel, joints, args.speed)
                elif k == RIGHT:
                    joints[sel] = min(arm_io.LIMITS[sel][1], joints[sel] + 1.0)
                    msg = _nudge(sel, joints, args.speed)
                elif k == SHIFT_LEFT:
                    joints[sel] = max(arm_io.LIMITS[sel][0], joints[sel] - 5.0)
                    msg = _nudge(sel, joints, args.speed)
                elif k == SHIFT_RIGHT:
                    joints[sel] = min(arm_io.LIMITS[sel][1], joints[sel] + 5.0)
                    msg = _nudge(sel, joints, args.speed)
                else:
                    msg = f"special 0x{k:02x} ignored"
    except KeyboardInterrupt:
        print()
        print("jog > KeyboardInterrupt, sending rest ...")
        try:
            arm_io.send_cmd("rest", timeout=30.0)
        except TimeoutError:
            pass


def _nudge(sel, joints, speed):
    try:
        arm_io.send_cmd("move",
                        joints={str(sel): round(joints[sel], 2)},
                        speed=speed,
                        timeout=10.0)
        return f"moved J{sel} to {joints[sel]:+.2f}"
    except TimeoutError as e:
        return f"nudge timeout: {e}"


if __name__ == "__main__":
    main()
