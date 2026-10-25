"""Arm moved by hand (motors off, no arm_server): keep the CAN adapter open and save a pose whenever asked.
Write a letter (A-D) into robotics/mark_request.txt; the pose is saved as vial_<X>_final_position and the result
written to robotics/mark_result.txt. Stop by writing 'quit'. (One process keeps the adapter: reopening it per read
can wedge it.)"""
import sys
import time
from pathlib import Path

ROB = Path(__file__).resolve().parents[2] / "robotics"
sys.path.insert(0, str(ROB))
import markers as T  # noqa: E402
from spots import save_spot  # noqa: E402

REQ, RES = ROB / "mark_request.txt", ROB / "mark_result.txt"
REQ.unlink(missing_ok=True)
q = T.hand_joints()
print("reader ready, arm at " + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 7)), flush=True)
while True:
    if REQ.exists():
        cmd = REQ.read_text().strip().upper()
        REQ.unlink(missing_ok=True)
        if cmd == "QUIT":
            break
        try:
            q = T.hand_joints()
            save_spot(f"vial_{cmd}_final_position", q)
            msg = f"saved vial_{cmd}_final_position = " + "  ".join(f"J{j}={q[j]:.1f}" for j in range(1, 7))
        except Exception as e:  # noqa: BLE001
            msg = f"FAILED {cmd}: {e}"
        RES.write_text(msg)
        print(msg, flush=True)
    time.sleep(0.1)
