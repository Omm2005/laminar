"""Arm moved by hand (motors off, no arm_server running): read the joint encoders and save the pose as
vial_<X>_final_position. python mark_hand.py A"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "robotics"))
import markers as T  # noqa: E402
from spots import save_spot  # noqa: E402

vial = sys.argv[1].upper()
q = T.hand_joints()
save_spot(f"vial_{vial}_final_position", q)
print(f"saved vial_{vial}_final_position (encoders, arm moved by hand) = "
      + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 7)))
