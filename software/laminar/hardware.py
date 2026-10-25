"""One switch between fake and real hardware. LAMINAR_FAKE=0 selects the real arm (robotics/arm_server.py +
the side camera); LAMINAR_CAM picks the side camera (default 0)."""
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
    level: LevelTracker | None      # the fakes' tube; the real skills read the side camera themselves
    skills: Skills
    bench: dict

    def close(self) -> None:
        """Release the camera (real hardware). Safe to call more than once."""
        close = getattr(self.skills, "close", None)
        if close:
            close()


def make_system(fake: bool | None = None, time_scale: float = 1.0, faults=(), bench_path=None, seed: int = 0) -> System:
    if fake is None:
        fake = os.environ.get("LAMINAR_FAKE", "1") != "0"
    bench = load_bench(bench_path)
    if not fake:
        from .real_skills import RealArm, RealSkills
        skills = RealSkills(cam=os.environ.get("LAMINAR_CAM", "0"))
        return System(RealArm(skills.client), None, skills, bench)
    arm = FakeArm(q0=pose(bench, "home"), time_scale=time_scale, workspace_m=workspace_m(bench))
    level = FakeLevelTracker(seed=seed)
    level.start(curve=bench["level_curve"][bench["station"]["tube_type"]])
    skills = FakeSkills(arm, level, bench, faults=set(faults), seed=seed)
    return System(arm, level, skills, bench)
