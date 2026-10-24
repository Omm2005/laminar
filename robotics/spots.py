"""Load and save taught spots (robotics/spots.yaml).

A "spot" is a named arm pose: 6 joint angles in degrees, follower frame (same as arm_server.py).
    slot_A..slot_D : pose at the end of each grab recording (tube held, still in the stand)
    well_1..well_4 : pour-ready pose over each well (tube upright, lowered, lip just above the well)
    vial_*         : named vial poses, e.g. vial_A_initial_position (where every run with vial A starts)
"""
from __future__ import annotations

import csv
from pathlib import Path as FilePath

import yaml

SPOTS_FILE = FilePath(__file__).resolve().parent / "spots.yaml"
GROUP_PREFIX = {"slots": "slot_", "wells": "well_", "vials": "vial_"}   # spots.yaml group -> name prefix
GROUPS = tuple(GROUP_PREFIX)


def load_spots(path: FilePath = SPOTS_FILE) -> dict[str, dict[int, float] | None]:
    """Read spots.yaml.
    Output: flat dict {"slot_A": {1: deg, ..., 6: deg} or None, "well_1": ..., ...}"""
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, dict[int, float] | None] = {}
    for group in GROUPS:
        for name, q in (data.get(group) or {}).items():
            out[name] = None if q is None else {i + 1: float(q[i]) for i in range(6)}
    return out


def get_spot(name: str, path: FilePath = SPOTS_FILE) -> dict[int, float]:
    """Joint angles of one spot. Raises a clear error if it hasn't been taught yet."""
    spots = load_spots(path)
    if name not in spots:
        raise KeyError(f"unknown spot {name!r}; known: {', '.join(spots)}")
    if spots[name] is None:
        raise ValueError(f"spot {name!r} is not taught yet (null in spots.yaml)")
    return spots[name]


def save_spot(name: str, q: dict[int, float], path: FilePath = SPOTS_FILE) -> None:
    """Write one spot into spots.yaml, keeping every other spot.
    Inputs: name like "well_2" or "slot_A"; q = joint angles in degrees {1..6}."""
    group = next((g for g, prefix in GROUP_PREFIX.items() if name.startswith(prefix)), None)
    if group is None:
        raise ValueError("spot names must start with " + ", ".join(GROUP_PREFIX.values()))
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault(group, {})[name] = [round(float(q[i]), 3) for i in range(1, 7)]
    # Rewrite by hand so each pose stays on one readable line: well_2: [-31.0, 100.0, ...]
    header = "".join(line + "\n" for line in path.read_text().splitlines() if line.startswith("#"))
    body = ""
    for grp in GROUPS:
        if grp == "vials" and not data.get(grp):
            continue
        body += f"{grp}:\n"
        for spot, val in (data.get(grp) or {}).items():
            body += f"  {spot}: {'null' if val is None else '[' + ', '.join(str(v) for v in val) + ']'}\n"
    rest = {k: v for k, v in data.items() if k not in GROUPS}   # e.g. vision.py's well_offsets
    if rest:
        body += yaml.safe_dump(rest, sort_keys=False, default_flow_style=None)
    path.write_text(header + body)


def spot_from_recording(csv_path: str | FilePath, column: str = "command") -> dict[int, float]:
    """Joint angles in the LAST row of a teleop recording (data.csv from recorder.py).

    Inputs
        csv_path : path to recordings/<run>/data.csv
        column   : "command" (what the follower was told; default, matches arm_server's frame
                   exactly) or "state" (what the encoders measured)
    Output
        {1..6: degrees}. Use it as slot_X: the pose a grab ends in.
    """
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{csv_path} has no data rows")
    last = rows[-1]
    return {i: float(last[f"{column}.J{i}"]) for i in range(1, 7)}
