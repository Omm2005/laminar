"""During teleop: save the follower's current pose as vial_<X>_final_position and the wrist-camera reference for
container x (servo.py). python teach_pour.py b <teleop log>"""
import re
import sys

sys.path.insert(0, r"C:SERSMOGHDOWNLOADSaMINAR-MAINOBOTICS")
import servo  # noqa: E402
from spots import save_spot  # noqa: E402

letter, log = sys.argv[1].lower(), sys.argv[2]
last = open(log, encoding="utf-8", errors="replace").read().split("\r")[-1]
q = {int(j): float(v) for j, v in re.findall(r"J(\d)=\s*(-?\d+\.\d)", last)}
if len(q) != 7:
    sys.exit(f"could not parse a pose from: {last!r}")
ref = servo.teach(letter, {j: q[j] for j in range(1, 7)})
save_spot(f"vial_{letter.upper()}_final_position", q)
print(f"saved vial_{letter.upper()}_final_position = " + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 8)))
print(f"wrist reference for {letter}: markers " + ", ".join(f"{i} at ({p[0]:.0f}, {p[1]:.0f})"
                                                           for i, p in ref["markers_px"].items()))
