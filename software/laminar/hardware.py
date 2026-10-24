"""One switch between fake and real hardware. LAMINAR_FAKE=0 selects the real arm once it exists."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .arm import Arm, FakeArm
from .bench import load_bench, pose, workspace_m
from .level import FakeLevelTracker, LevelTracker
from .skills import FakeSkills, Skills


@dataclass
class System:
    arm: Arm
    level: LevelTracker
    skills: Skills
    bench: dict


def make_system(fake: bool | None = None, time_scale: float = 1.0, faults=(), bench_path=None, seed: int = 0) -> System:
    if fake is None:
        fake = os.environ.get("LAMINAR_FAKE", "1") != "0"
    bench = load_bench(bench_path)
    if not fake:
        raise NotImplementedError("real hardware not wired yet: needs robotics/arm.py and sense/level.py")
    arm = FakeArm(q0=pose(bench, "home"), time_scale=time_scale, workspace_m=workspace_m(bench))
    level = FakeLevelTracker(seed=seed)
    level.start(curve=bench["level_curve"][bench["station"]["tube_type"]])
    skills = FakeSkills(arm, level, bench, faults=set(faults), seed=seed)
    return System(arm, level, skills, bench)
