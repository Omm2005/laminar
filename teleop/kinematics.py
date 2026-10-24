"""Forward kinematics for the reBot Arm B601-RS from the vendor URDF (model/rebot_b601_rs.urdf).

Joint angles are the follower motor angles in degrees from the zero pose (same frame as teleop/arm_server).
The tool frame is gripper_end; its z axis is the wrist-roll (approach) axis.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

URDF = Path(__file__).resolve().parent / "model" / "rebot_b601_rs.urdf"


def _rpy(r, p, y):
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def _axis_angle(axis, a):
    x, y, z = axis / np.linalg.norm(axis)
    c, s, t = np.cos(a), np.sin(a), 1 - np.cos(a)
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


def _load():
    chain = []
    for j in ET.parse(URDF).getroot().findall("joint"):
        if j.get("type") not in ("revolute", "fixed") or j.get("name").startswith("joint_"):
            continue
        o = j.find("origin")
        xyz = np.array([float(v) for v in o.get("xyz").split()])
        rpy = [float(v) for v in o.get("rpy").split()]
        a = j.find("axis")
        axis = np.array([float(v) for v in a.get("xyz").split()]) if a is not None else None
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = _rpy(*rpy), xyz
        chain.append((j.get("name"), j.get("type"), T, axis))
    return chain


CHAIN = _load()


def fk(q_deg):
    """q_deg: {1..6: degrees}. Returns (position xyz in m, 3x3 rotation of the tool frame) in the base frame."""
    T = np.eye(4)
    i = 0
    for name, jtype, T0, axis in CHAIN:
        T = T @ T0
        if jtype == "revolute":
            i += 1
            R = np.eye(4)
            R[:3, :3] = _axis_angle(axis, np.radians(q_deg.get(i, 0.0)))
            T = T @ R
    return T[:3, 3], T[:3, :3]


def describe(q_deg):
    p, R = fk(q_deg)
    return (f"tool at x={p[0]:+.3f} y={p[1]:+.3f} z={p[2]:+.3f} m, approach(z)={np.round(R[:, 2], 2)}, "
            f"jaw-open(y)={np.round(R[:, 1], 2)}")


if __name__ == "__main__":
    for label, q in {
        "rest": {},
        "demo grasp (56s)": {1: -31, 2: 100, 3: 11, 4: 88, 5: 29, 6: -5},
        "demo over container (92s)": {1: 6, 2: 121, 3: 37, 4: 88, 5: 63, 6: -12},
        "scan pose J1=-15": {1: -15, 2: 47, 3: 0, 4: 40, 5: 2, 6: -7},
    }.items():
        print(f"{label:28s} {describe(q)}")
