"""Gravity torques for the reBot B601-RS from the vendor URDF's link masses and centres of mass.

gravity_torque(q) is the torque each joint motor must apply to hold the arm still at pose q (follower motor
degrees, same frame as kinematics.py / arm_server.py). arm_server feeds it forward so the position loop only
corrects small errors: less sag under load, and the same tracking with softer, smoother gains.

    python dynamics.py                # torques at a few poses
"""
import xml.etree.ElementTree as ET

import numpy as np

from kinematics import URDF, _axis_angle, _rpy

G = np.array([0.0, 0.0, -9.81])


def _load_tree():
    root = ET.parse(URDF).getroot()
    links = {}
    for l in root.findall("link"):
        i = l.find("inertial")
        if i is not None:
            links[l.get("name")] = (float(i.find("mass").get("value")),
                                    np.array([float(v) for v in i.find("origin").get("xyz").split()]))
    joints = []
    for j in root.findall("joint"):
        o = j.find("origin")
        T = np.eye(4)
        T[:3, :3] = _rpy(*[float(v) for v in o.get("rpy").split()])
        T[:3, 3] = [float(v) for v in o.get("xyz").split()]
        a = j.find("axis")
        axis = np.array([float(v) for v in a.get("xyz").split()]) if a is not None else None
        joints.append((j.get("name"), j.get("type"), j.find("parent").get("link"), j.find("child").get("link"),
                       T, axis))
    return links, joints


LINKS, JOINTS = _load_tree()
ARM_JOINTS = [f"joint{i}" for i in range(1, 7)]


def _frames(q_deg):
    """World transform of every link, plus each arm joint's world origin and axis."""
    T = {"base_link": np.eye(4)}
    jinfo = {}
    for name, jtype, parent, child, T0, axis in JOINTS:   # vendor URDF lists joints parent-first
        Tj = T[parent] @ T0
        if name in ARM_JOINTS:
            k = ARM_JOINTS.index(name) + 1
            jinfo[k] = (Tj[:3, 3].copy(), Tj[:3, :3] @ (axis / np.linalg.norm(axis)), child)
            R = np.eye(4)
            R[:3, :3] = _axis_angle(axis, np.radians(q_deg.get(k, 0.0)))
            Tj = Tj @ R
        T[child] = Tj                                       # gripper fingers: prismatic, held at 0
    return T, jinfo


def _descendants(link):
    out, stack = set(), [link]
    while stack:
        l = stack.pop()
        out.add(l)
        stack += [c for _, _, p, c, _, _ in JOINTS if p == l]
    return out


DOWNSTREAM = {}


def gravity_torque(q_deg, payload_kg: float = 0.0) -> dict[int, float]:
    """N.m per arm joint (1-6) needed to hold pose q against gravity. payload_kg: extra mass at the gripper."""
    T, jinfo = _frames(q_deg)
    tau = {}
    for k, (p_j, axis, child) in jinfo.items():
        if k not in DOWNSTREAM:
            DOWNSTREAM[k] = _descendants(child)
        t = 0.0
        for link in DOWNSTREAM[k]:
            if link not in LINKS:
                continue
            m, com = LINKS[link]
            if link == "gripper_end":
                m += payload_kg
            p = T[link][:3, :3] @ com + T[link][:3, 3]
            t += axis @ np.cross(p - p_j, m * G)            # torque gravity puts on joint k
        tau[k] = -t                                          # the motor must cancel it
    return tau


if __name__ == "__main__":
    for q in ({i: 0.0 for i in range(1, 7)},
              {1: 14.5, 2: 95.8, 3: 17.3, 4: 90.0, 5: -1.1, 6: -6.1},
              {1: -13.0, 2: 49.2, 3: 1.7, 4: 60.0, 5: 22.7, 6: -11.2}):
        print({k: round(v, 2) for k, v in gravity_torque(q).items()}, "at", q)
