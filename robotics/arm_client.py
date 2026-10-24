"""Send planned Paths to the real arm through robotics/arm_server.py.

arm_server.py holds the arm and executes one command at a time from robotics/arm_cmd.json, writing
progress to robotics/arm_status.json. This client writes those files for you.

Two ways to run a Path:

    mode="moves"  (works with arm_server.py as it is today)
        Sends the Path as a handful of joint "move" commands, one every CHUNK_MM of gripper travel.
        arm_server smooths each move on its own, so the arm briefly slows at every chunk boundary.
        Good enough to test reach and accuracy; a little jerky for carrying liquid.

    mode="path"   (needs a small "path" op added to arm_server.py; see robotics/README.md)
        Sends the whole Path at once; arm_server plays every target at its control rate.
        Smooth, and the right mode for carrying liquid.

Units: joint angles in degrees, {1..6}, same frame as arm_server.py and motion.py.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path as FilePath

import numpy as np

from motion import Path, fk_mm

HERE = FilePath(__file__).resolve().parent
CMD_FILE = HERE / "arm_cmd.json"          # same files arm_server.py uses (it lives in this folder)
STATUS_FILE = HERE / "arm_status.json"
CHUNK_MM = 10.0                            # mode="moves": one move command per this much gripper travel
PICKUP_TIMEOUT_S = 3.0                     # arm_server must acknowledge a new command within this time


class ArmClient:
    """Minimal client for arm_server.py.

    Inputs (constructor)
        dry_run : True = print what would be sent and change nothing. Use this first, every time.
        timeout_s : how long to wait for any single command before giving up
    """

    def __init__(self, dry_run: bool = False, timeout_s: float = 120.0):
        self.dry_run = dry_run
        self.timeout_s = timeout_s

    # ---- reading the arm ---------------------------------------------------------------------

    def status(self) -> dict | None:
        """Latest arm_status.json as a dict, or None if arm_server isn't running / file mid-write."""
        try:
            return json.loads(STATUS_FILE.read_text())
        except (ValueError, OSError):
            return None

    def current_joints(self) -> dict[int, float]:
        """Joint angles (degrees, {1..6}) the arm is currently commanded to.
        We use the COMMANDED angles, not the measured ones, so a new plan starts exactly where the
        last one ended (the measured angles differ by a little servo sag)."""
        st = self.status()
        if not st or "command" not in st:
            raise RuntimeError("arm_server.py is not running (no robotics/arm_status.json). Start it first.")
        return {i: float(st["command"][f"J{i}"]) for i in range(1, 7)}

    # ---- sending commands ----------------------------------------------------------------------

    def _send(self, cmd: dict) -> dict:
        """Write one command atomically, then wait for arm_server to finish it. Returns the status."""
        cmd = {"id": time.time_ns(), **cmd}
        if self.dry_run:
            print("DRY RUN would send:", json.dumps(cmd)[:200])
            return {"result": "dry run"}
        tmp = CMD_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cmd))
        for _ in range(50):  # arm_server may be reading the file right now (Windows locks it)
            try:
                os.replace(tmp, CMD_FILE)
                break
            except PermissionError:
                time.sleep(0.01)
        start = time.time()
        end = start + self.timeout_s
        picked_up = False
        while time.time() < end:
            st = self.status()
            if st and st.get("id") == cmd["id"]:
                picked_up = True
            elif not picked_up and time.time() - start > PICKUP_TIMEOUT_S:
                # arm_server answers a new command within a few ticks; silence means it isn't running
                raise RuntimeError(f"arm_server did not pick up the {cmd['op']} command within "
                                   f"{PICKUP_TIMEOUT_S:.0f} s. Is robotics/arm_server.py running?")
            if st and st.get("id") == cmd["id"] and not st.get("busy") and st.get("result") != "running":
                if str(st.get("result", "")).startswith("STOPPED"):
                    raise RuntimeError(f"arm_server stopped: {st['result']}")
                return st
            time.sleep(0.05)
        raise TimeoutError(f"arm_server did not finish command {cmd['op']} within {self.timeout_s:.0f} s")

    def run(self, path: Path, mode: str = "moves") -> dict:
        """Execute a planned Path on the arm.

        Inputs
            path : a Path from motion.plan_lift / plan_lower / plan_travel
            mode : "moves" (works today, slightly jerky) or "path" (smooth; needs the arm_server op)
        Output
            the final arm_status.json dict
        Safety
            Refuses to start if the arm is not where the Path starts (within 1 deg per joint), so a
            stale plan can never yank the arm across the table.
        """
        if not self.dry_run:
            q_now = self.current_joints()
            off = max(abs(q_now[j] - path.joints[0][j]) for j in range(1, 7))
            if off > 1.0:
                raise RuntimeError(f"{path.label}: arm is {off:.1f} deg away from the path's start pose; re-plan")
        print(path.summary())

        if mode == "path":
            pts = [{str(j): round(q[j], 3) for j in range(1, 7)} for q in path.joints]
            hz = 1.0 / (path.times_s[1] - path.times_s[0]) if len(path.times_s) > 1 else 50.0
            return self._send({"op": "path", "points": pts, "hz": hz})

        if mode != "moves":
            raise ValueError("mode must be 'moves' or 'path'")

        # mode="moves": pick targets every CHUNK_MM of gripper travel, plus the final target.
        last_sent = None
        last_p = fk_mm(path.joints[0])[0]
        result = {"result": "nothing to do"}
        t_prev = 0.0
        for k, q in enumerate(path.joints[1:], start=1):
            p = fk_mm(q)[0]
            is_last = k == len(path.joints) - 1
            if np.linalg.norm(p - last_p) < CHUNK_MM and not is_last:
                continue
            # arm_server move duration = 1.5 * (largest joint change) / speed.
            # Choose speed so this chunk takes as long as the Path says it should.
            prev = last_sent or path.joints[0]
            biggest = max(abs(q[j] - prev[j]) for j in range(1, 7))
            dt = max(0.5, path.times_s[k] - t_prev)
            speed = max(1.0, 1.5 * biggest / dt)
            result = self._send({"op": "move", "joints": {str(j): round(q[j], 3) for j in range(1, 7)},
                                 "speed": round(speed, 2)})
            last_sent, last_p, t_prev = q, p, path.times_s[k]
        return result
