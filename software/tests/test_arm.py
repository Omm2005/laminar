import numpy as np
import pytest

from laminar.arm import ArmError, ArmStopped, FakeArm


def test_move_q_reaches_target():
    arm = FakeArm(time_scale=0)
    target = [0.1, 0.5, 1.0, 0.0, 0.2, -0.3]
    arm.move_q(target, 1.0)
    assert np.allclose(arm.get_q(), target)


def test_rejects_joint_outside_urdf_limits():
    arm = FakeArm(time_scale=0)
    with pytest.raises(ArmError, match="joint2"):
        arm.move_q([0, -0.5, 1.0, 0, 0, 0], 1.0)  # joint2 lower limit is 0


def test_rejects_wrong_joint_count():
    with pytest.raises(ArmError, match="6 joint"):
        FakeArm(time_scale=0).move_q([0, 0, 0], 1.0)


def test_move_pose_respects_workspace_box():
    arm = FakeArm(time_scale=0, workspace_m=(np.array([-0.4, -0.4, 0]), np.array([0.4, 0.4, 0.45])))
    T = np.eye(4)
    T[:3, 3] = [0.2, 0.0, 0.1]
    arm.move_pose(T, 1.0)
    T[:3, 3] = [0.9, 0.0, 0.1]
    with pytest.raises(ArmError, match="workspace"):
        arm.move_pose(T, 1.0)


def test_stop_blocks_all_motion_until_cleared():
    arm = FakeArm(time_scale=0)
    arm.stop()
    with pytest.raises(ArmStopped):
        arm.move_q([0, 0.5, 0.5, 0, 0, 0], 1.0)
    arm.clear_stop()
    arm.move_q([0, 0.5, 0.5, 0, 0, 0], 1.0)


def test_gripper_stalls_on_object():
    arm = FakeArm(time_scale=0)
    arm.gripper(30)
    assert not arm.gripper_stalled()
    arm.object_mm = 16
    arm.gripper(14)
    assert arm.gripper_stalled()


def test_grasp_slip_fault():
    arm = FakeArm(time_scale=0)
    arm.faults.add("grasp_slip")
    arm.object_mm = 16
    arm.gripper(14)
    assert not arm.gripper_stalled()
