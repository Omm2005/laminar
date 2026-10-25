import numpy as np
import pytest

import deliver as D
import markers as T


def test_parse_jobs():
    assert D.parse("A2c") == ("vial_A", 3) and D.parse("d2A") == ("vial_D", 1)
    with pytest.raises(SystemExit):
        D.parse("E2a")


def test_offsets_turn_with_the_tray(monkeypatch):
    pose = {"containers": {2: np.array([400.0, 100.0])}, "heading_deg": 90.0}
    monkeypatch.setitem(D.CONTAINER_OFFSET_MM, 2, (10.0, 0.0))   # 10 mm along the tray line
    assert np.allclose(D.container_target(pose, 2), [400.0, 110.0])


def test_carry_ends_over_the_container_with_the_vial_upright():
    import motion as M
    from paths import load
    xy = np.array([480.0, 300.0])
    (lift, travel, lower), qa = D.plan_carry("vial_A", xy)
    pa, Ra = M.fk_mm(qa)
    pb, Rb = M.fk_mm(lower.final_joints)
    assert np.linalg.norm(pb[:2] - xy) < 1.0 and abs(pb[2] - (pa[2] + D.ABOVE_MM)) < 1.0
    assert abs(Rb[2, 2] - Ra[2, 2]) < 0.02          # tilt unchanged: only the heading turned
