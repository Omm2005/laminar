"""Bring a held vial from wherever the arm is back into its slot: up to the carry plane, over the slot, down to
the grasp pose, then the teleop grab backwards (releases it, back to rest)."""
import sys
sys.path.insert(0, r"C:SERSMOGHDOWNLOADSaMINAR-MAINOBOTICS")
import numpy as np
import motion as M
import grab
from arm_client import ArmClient
from paths import load
vial = sys.argv[1]
c = ArmClient()
qa = {j: v for j, v in load(vial)["initial_position"].items() if j <= 6}
pa, Ra = M.fk_mm(qa)
q = c.current_joints(); p, R = M.fk_mm(q)
z_carry = pa[2] + M.TRANSFER_LIFT_MM
moves = [M.plan_line(q, [p[0], p[1], z_carry], None, speed_mm_s=M.DEFAULT_LIFT_SPEED, label="lift")]
moves.append(M.plan_line(moves[-1].final_joints, [pa[0], pa[1], z_carry], Ra, speed_mm_s=M.DEFAULT_TRAVEL_SPEED,
                         label="travel back over the slot"))
moves.append(M.plan_line(moves[-1].final_joints, pa, Ra, speed_mm_s=M.DEFAULT_LOWER_SPEED, label="lower into the slot"))
for m in moves:
    print(m.summary())
for m in moves:
    print("  " + str(c.run(m).get("result")))
end = moves[-1].final_joints
off = max(abs(end[j] - qa[j]) for j in range(1, 7))
if off > 1.0:
    print(f"  ending {off:.1f} deg from the grasp pose: joint move onto it")
    c._send({"op": "move", "joints": {str(j): v for j, v in qa.items()}, "speed": 10.0})
grab.play(vial, run=True, reverse=True)
print(vial, "back in its slot, arm at rest pose")
