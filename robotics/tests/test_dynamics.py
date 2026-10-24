"""Gravity model vs. torques the real motors reported while holding still (replay log, 2026-10-24)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dynamics import gravity_torque  # noqa: E402

# (pose, measured holding torque J2, J3, J4 in N.m)
MEASURED = [
    ({1: 14, 2: 111, 3: 20, 4: 88, 5: -3, 6: -2}, (-6.79, 3.72, 2.05)),
    ({1: 14, 2: 98, 3: 16, 4: 88, 5: -1, 6: -6}, (-5.72, 4.37, 2.04)),
    ({1: 14, 2: 71, 3: 26, 4: 51, 5: 0, 6: -6}, (-3.96, 6.38, 1.82)),
    ({1: -13, 2: 48, 3: 1, 4: 54, 5: 23, 6: -11}, (-0.96, 5.86, 2.09)),
]


@pytest.mark.parametrize("q, measured", MEASURED)
def test_gravity_matches_the_real_arm(q, measured):
    tau = gravity_torque(q)
    for j, m in zip((2, 3, 4), measured):
        assert tau[j] == pytest.approx(m, abs=0.8)      # right sign, right size


def test_vertical_axes_carry_no_gravity():
    tau = gravity_torque({1: 30, 2: 60, 3: 20, 4: 40, 5: 0, 6: 0})
    assert abs(tau[1]) < 1e-6 and abs(tau[6]) < 0.05
