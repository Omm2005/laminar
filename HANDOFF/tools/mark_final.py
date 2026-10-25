"""Save teleop's latest commanded follower pose as vial_<X>_final_position: python mark_final.py A <teleop output file>"""
import re
import sys

sys.path.insert(0, r"C:SERSMOGHDOWNLOADSaMINAR-MAINOBOTICS")
from spots import save_spot  # noqa: E402

vial, out = sys.argv[1].upper(), sys.argv[2]
last = open(out, encoding="utf-8", errors="replace").read().split("\r")[-1]
q = {int(j): float(v) for j, v in re.findall(r"J(\d)=\s*(-?\d+\.\d)", last)}
if len(q) != 7:
    sys.exit(f"could not parse a pose from: {last!r}")
name = f"vial_{vial}_final_position"
save_spot(name, q)
print(f"saved {name} = " + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 8)))
