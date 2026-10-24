"""Labelled arm motions recorded by teleop, one Python file each, replayed by grab.py.

Each file (e.g. paths/vial_A.py) is written by `python grab.py save ...` and holds:
    NAME             "vial_A"
    LABEL            what the motion does, in words
    SOURCE           the teleop recording it was cut from (local, not in git)
    HZ               replay rate, points per second
    INITIAL_POSITION {1..6: deg, 7: gripper deg}: where the motion ends (e.g. vial A held in its rack slot);
                     joints 1-6 are also in spots.yaml as <NAME>_initial_position
    POINTS           [[J1..J7], ...] degrees, follower frame (same as arm_server.py), from the first frame that
                     moves to INITIAL_POSITION, gripper included (it closes on the vial at the end)

    from paths import load, names
    p = load("vial_A")          # dict with the fields above
"""
from __future__ import annotations

import importlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def names() -> list[str]:
    """Every saved motion, e.g. ["vial_A", "vial_B"]."""
    return sorted(p.stem for p in HERE.glob("*.py") if p.stem != "__init__")


def load(name: str) -> dict:
    """One motion as a dict: name, label, source, hz, initial_position {1..7: deg}, points [{1..7: deg}, ...].
    Raises KeyError with the known names if it doesn't exist."""
    if name not in names():
        raise KeyError(f"no saved motion {name!r}; saved: {', '.join(names()) or 'none'}")
    m = importlib.import_module(f"paths.{name}")
    return {"name": m.NAME, "label": m.LABEL, "source": m.SOURCE, "hz": float(m.HZ),
            "initial_position": {int(k): float(v) for k, v in m.INITIAL_POSITION.items()},
            "points": [{j + 1: float(v) for j, v in enumerate(row)} for row in m.POINTS]}
