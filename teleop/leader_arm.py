"""FashionStar reBot Arm 102 leader (StarArm 102) reader, mapped into the B601-RS follower's motor frame.

Mapping derived from LeRobot's rebot_102_leader (joint_directions) and rebot_b601_follower RS profile
(joint_directions = -1): follower_motor_deg = scale * leader_raw_deg, measured from each arm's zero pose.
"""
import json
from pathlib import Path

from motorbridge_smart_servo import FashionStarServo

LEADER_PORT = "COM14"
LEADER_BAUD = 1_000_000
HERE = Path(__file__).resolve().parent
LEADER_CAL_FILE = HERE / "leader_calibration.json"
FOLLOWER_CAL_FILE = HERE / "follower_calibration.json"

# leader servo id -> (follower motor id, scale, follower motor-frame limits in degrees relative to zero pose)
MAPPING = {
    0: (1, +1.0, (-145.0, 145.0)),  # shoulder_pan
    1: (2, +1.0, (0.0, 170.0)),     # shoulder_lift
    2: (3, -1.0, (0.0, 180.0)),     # elbow_flex
    3: (4, -1.0, (-80.0, 90.0)),    # wrist_flex
    4: (5, -1.0, (-90.0, 90.0)),    # wrist_yaw
    5: (6, +1.0, (-90.0, 90.0)),    # wrist_roll
    6: (7, +6.0, (0.0, 270.0)),     # gripper (leader travel is ~45 deg, follower ~270 deg)
}


def unwrap(raw, center):
    """Remove whole turns so the angle lies within +-180 deg of center (servos report multi-turn angles)."""
    return raw - round((raw - center) / 360.0) * 360.0


class LeaderArm:
    def __init__(self, port=LEADER_PORT, baudrate=LEADER_BAUD):
        self.bus = FashionStarServo(port, baudrate=baudrate)
        self.ids = list(MAPPING)
        missing = [i for i in self.ids if not self.bus.ping(i)]
        if missing:
            self.bus.close()
            raise RuntimeError(f"Leader servos not responding: {missing}")
        for i in self.ids:
            self.bus.unlock(i)  # leader is moved by hand; make sure no servo holds torque
        for i in self.ids:
            self.bus.reset_multi_turn(i)

    def close(self):
        self.bus.close()

    def read_raw(self):
        """Raw servo angles in degrees, {servo_id: deg}. Raises if any servo is missing."""
        result = self.bus.sync_monitor(self.ids)
        missing = [i for i in self.ids if result.get(i) is None]
        if missing:
            raise RuntimeError(f"no reading from leader servos {missing}")
        return {i: result[i].angle_deg for i in self.ids}

    def read_follower_targets(self):
        """Leader pose as follower targets: {follower motor id: degrees relative to follower zero pose}."""
        raw = self.read_raw()
        out = {}
        for sid, (mid, scale, (lo, hi)) in MAPPING.items():
            center = (lo + hi) / 2.0 / scale
            out[mid] = min(hi, max(lo, scale * unwrap(raw[sid], center)))
        return out


def load_follower_zero():
    if not FOLLOWER_CAL_FILE.exists():
        raise SystemExit("No follower calibration - run: python calibrate_follower.py")
    data = json.loads(FOLLOWER_CAL_FILE.read_text())
    return {int(k): float(v) for k, v in data["zero_deg"].items()}
