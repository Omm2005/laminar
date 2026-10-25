import numpy as np
import pytest

import deliver as D
import markers as T


def test_parse_jobs():
    assert D.parse("A2c") == ("vial_A", 3) and D.parse("c2B") == ("vial_C", 2)
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


def _tray(xy, th):
    """A tray pose like markers.find_tray gives, from the calibrated layout."""
    lay = T.load_calib()["layout_mm"]
    R = T.rot(th)
    return {"xy": np.array(xy, float), "heading_deg": th, "rms_mm": 0.0, "seen": sorted(lay),
            "containers": {T.MARKERS[i]: R @ np.asarray(lay[i]) + np.array(xy, float) for i in lay}}


def _tray_at_the_final_positions():
    import math
    import motion as M
    from spots import load_spots
    sp = load_spots()
    tips = np.array([M.fk_mm(sp[D.FINAL_SPOT.format(v)])[0][:2] for v in "ABCD"])
    d = tips[3] - tips[0]
    return _tray(tips.mean(axis=0) + [60.0, 0.0], math.degrees(math.atan2(d[1], d[0])))


def test_learned_offsets_give_back_the_taught_pour_joints_when_the_tray_has_not_moved():
    import motion as M
    pose = _tray_at_the_final_positions()
    offs = D.learn_offsets(pose)
    assert sorted(offs) == [1, 2, 3, 4] and [offs[c]["marker"] for c in offs] == [10, 11, 12, 13]
    for c in offs:
        p, R, seed = D.pour_pose(pose, c, offs)
        Rg, q = D.reachable_pour(p, R, seed)
        assert np.allclose(Rg, R)                      # no heading give needed at the taught place
        assert max(abs(q[j] - offs[c]["joints"][j]) for j in range(1, 7)) < 0.5


def test_pour_pose_moves_and_turns_with_the_tray():
    import motion as M
    pose = _tray_at_the_final_positions()
    offs = D.learn_offsets(pose)
    moved = _tray(pose["xy"] + [20.0, -15.0], pose["heading_deg"] + 10.0)
    p0, R0, _ = D.pour_pose(pose, 2, offs)
    p1, R1, _ = D.pour_pose(moved, 2, offs)
    # the tip keeps its place relative to the marker, in the tray's frame, and its height
    rel0 = T.rot(-pose["heading_deg"]) @ (p0[:2] - pose["containers"][2])
    rel1 = T.rot(-moved["heading_deg"]) @ (p1[:2] - moved["containers"][2])
    assert np.allclose(rel0, rel1) and abs(p0[2] - p1[2]) < 1e-9
    # the gripper turned with the tray, about vertical only
    assert np.allclose(R1, T.yaw(10.0) @ R0)


def test_offsets_round_trip_through_the_file(tmp_path):
    pose = _tray_at_the_final_positions()
    offs = D.learn_offsets(pose)
    f = tmp_path / "container_offsets.yaml"
    D.save_offsets(offs, pose, f)
    back = D.load_offsets(f)
    assert sorted(back) == sorted(offs)
    assert np.allclose(back[3]["R_tray"], offs[3]["R_tray"]) and back[3]["dxy_mm"] == offs[3]["dxy_mm"]


def test_a_moved_tray_still_plans_a_pour_carry():
    pose = _tray_at_the_final_positions()
    offs = D.learn_offsets(pose)
    moved = _tray(pose["xy"] + [0.0, -40.0], pose["heading_deg"])
    p, R, seed = D.pour_pose(moved, 1, offs)
    moves, qa, qp = D.plan_pour_carry("vial_A", p, R, seed)
    assert [m.label.split()[0] for m in moves] == ["lift", "travel", "lower", "turn"]
