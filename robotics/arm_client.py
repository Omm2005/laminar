"""Send planned Paths to the real arm through robotics/arm_server.py.

arm_server.py holds the arm and executes one command at a time from robotics/arm_cmd.json, writing
progress to robotics/arm_status.json. This client writes those files for you.

Two ways to run a Path:

    mode="path"   (default) sends the whole Path at once; arm_server plays every target continuously
        at its control rate. Smooth: the right mode for carrying liquid.

    mode="moves"  (fallback)
        Sends the Path as a handful of joint "move" commands, one every CHUNK_MM of gripper travel.
        arm_server smooths each move on its own, so the arm briefly slows at every chunk boundary.
        Good enough to test reach and accuracy; a little jerky for carrying liquid.

Live pour control: start_tilt_stream() hands the pour roll (J6) to the caller, set_tilt() streams
offsets through robotics/arm_tilt.json, end_tilt_stream() returns it upright. arm_server tilts the tube
back on its own if updates stop for timeout_s.

Units: joint angles in degrees, {1..6}, same frame as arm_server.py and motion.py.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path as FilePath

import numpy as np

import run_log
from motion import Path, fk_mm

HERE = FilePath(__file__).resolve().parent
CMD_FILE = HERE / "arm_cmd.json"          # same files arm_server.py uses (it lives in this folder)
STATUS_FILE = HERE / "arm_status.json"
TILT_FILE = HERE / "arm_tilt.json"         # live pour-roll offsets for arm_server's tilt_stream op
POSE_FILE = HERE / "arm_pose.json"         # live J1-J6 targets for arm_server's pose_stream op
CHUNK_MM = 10.0                            # mode="moves": one move command per this much gripper travel
PICKUP_TIMEOUT_S = 3.0                     # arm_server must acknowledge a new command within this time
STATUS_STALE_S = 3.0                       # idle arm_server rewrites its status every 0.5 s; older = not running


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
        """Latest arm_status.json as a dict, or None if arm_server isn't running. The server replaces the file
        atomically, but on Windows a read during the replace fails for a moment: retry for up to ~0.2 s (a single
        failed read once ended a run with 'arm_server.py is not running' while it was holding the arm)."""
        for _ in range(10):
            try:
                return json.loads(STATUS_FILE.read_text())
            except (ValueError, OSError):
                time.sleep(0.02)
        return None

    def current_joints(self) -> dict[int, float]:
        """Joint angles (degrees, {1..6}) the arm is currently commanded to.
        We use the COMMANDED angles, not the measured ones, so a new plan starts exactly where the
        last one ended (the measured angles differ by a little servo sag)."""
        st = self.status()
        if not st or "command" not in st:
            raise RuntimeError("arm_server.py is not running (no robotics/arm_status.json). Start it first.")
        # arm_server rewrites the status every 0.5 s while idle; an old file is left over from a server that
        # exited (15 min idle timeout, a fault, or rest), and its joint angles are no longer true
        age = time.time() - STATUS_FILE.stat().st_mtime
        if age > STATUS_STALE_S and not st.get("busy"):
            raise RuntimeError(f"arm_server.py is not running: arm_status.json is {age:.0f} s old "
                               f"(last result: {st.get('result')!r}). It stops itself after "
                               "15 min idle or on a fault; restart it.")
        return {i: float(st["command"][f"J{i}"]) for i in range(1, 7)}

    # ---- sending commands ----------------------------------------------------------------------

    @staticmethod
    def _write_atomic(path: FilePath, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        for _ in range(50):  # arm_server may be reading the file right now (Windows locks it)
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.005)

    def _send(self, cmd: dict, wait: bool = True) -> dict:
        """Write one command atomically, then wait for arm_server to finish it (wait=False: only until it
        is picked up). Returns the status."""
        cmd = {"id": time.time_ns(), **cmd}
        if self.dry_run:
            print("DRY RUN would send:", json.dumps(cmd)[:200])
            return {"result": "dry run"}
        run_log.started()  # every process that moves the arm is a Tiger run (no-op without TIGER_DATABASE_URL)
        try:
            return self._send_and_wait(cmd, wait)
        except (RuntimeError, TimeoutError) as e:
            run_log.failed(str(e))
            raise

    def _send_and_wait(self, cmd: dict, wait: bool) -> dict:
        self._write_atomic(CMD_FILE, cmd)
        self._last_id = cmd["id"]
        start = time.time()
        end = start + self.timeout_s
        picked_up = False
        while time.time() < end:
            st = self.status()
            if st and st.get("id") == cmd["id"]:
                picked_up = True
                if not wait:
                    return st
            elif not picked_up and time.time() - start > PICKUP_TIMEOUT_S:
                # arm_server answers a new command within a few ticks; silence means it isn't running
                raise RuntimeError(f"arm_server did not pick up the {cmd['op']} command within "
                                   f"{PICKUP_TIMEOUT_S:.0f} s. Is robotics/arm_server.py running?")
            if st and st.get("id") == cmd["id"] and not st.get("busy") and st.get("result") != "running":
                if str(st.get("result", "")).startswith("STOPPED"):
                    raise RuntimeError(f"arm_server stopped: {st['result']}")
                if "refused" in str(st.get("result", "")):
                    raise RuntimeError(f"arm_server {st['result']}")
                return st
            time.sleep(0.05)
        raise TimeoutError(f"arm_server did not finish command {cmd['op']} within {self.timeout_s:.0f} s")

    def wait_idle(self) -> dict:
        """Block until the last command sent is finished. Returns the status."""
        end = time.time() + self.timeout_s
        while time.time() < end:
            st = self.status()
            if st and st.get("id") == getattr(self, "_last_id", None) and not st.get("busy")                     and st.get("result") != "running":
                return st
            time.sleep(0.05)
        raise TimeoutError("arm_server did not finish in time")

    # ---- live pour control ---------------------------------------------------------------------

    def start_tilt_stream(self, joint: int = 6, max_rate_deg_s: float = 120.0, timeout_s: float = 0.5) -> float:
        """Hand joint `joint` to set_tilt(). Returns the base angle (degrees) offsets are measured from."""
        base = self.current_joints()[joint] if not self.dry_run else 0.0
        self._tilt_seq = 0
        self._write_atomic(TILT_FILE, {"seq": -1, "deg": 0.0, "done": False})
        self._send({"op": "tilt_stream", "joint": joint, "base": base, "max_rate_deg_s": max_rate_deg_s,
                    "timeout_s": timeout_s}, wait=False)
        return base

    def set_tilt(self, offset_deg: float, done: bool = False) -> None:
        """Pour roll target, degrees from the base angle. Call at least every timeout_s."""
        self._tilt_seq += 1
        if self.dry_run:
            return
        self._write_atomic(TILT_FILE, {"seq": self._tilt_seq, "deg": float(offset_deg), "done": bool(done)})

    def end_tilt_stream(self) -> dict:
        """Tilt back upright and wait until arm_server has finished the stream."""
        self.set_tilt(0.0, done=True)
        return {"result": "dry run"} if self.dry_run else self.wait_idle()

    def start_table_tilt(self, table: list[dict[int, float]], step_deg: float, max_rate_deg_s: float = 30.0,
                         joint_rate_deg_s: float = 45.0, timeout_s: float = 0.5) -> None:
        """Hand the pour tilt to arm_server: J1-J6 per step_deg of tilt; then set_tilt(deg) streams the angle and
        end_tilt_stream() tilts back and ends it."""
        self._tilt_seq = 0
        self._write_atomic(TILT_FILE, {"seq": -1, "deg": 0.0, "done": False})
        self._send({"op": "table_tilt", "table": [[round(r[j], 4) for j in range(1, 7)] for r in table],
                    "step_deg": step_deg, "max_rate_deg_s": max_rate_deg_s, "joint_rate_deg_s": joint_rate_deg_s,
                    "timeout_s": timeout_s}, wait=False)

    def start_pose_stream(self, max_rate_deg_s: float = 60.0, timeout_s: float = 0.5) -> dict[int, float]:
        """Hand J1-J6 to set_pose(). Returns the start pose (degrees): where the arm goes back when the stream ends."""
        base = self.current_joints() if not self.dry_run else {j: 0.0 for j in range(1, 7)}
        self._pose_seq = 0
        self._write_atomic(POSE_FILE, {"seq": -1, "joints": {str(j): v for j, v in base.items()}, "done": False})
        self._send({"op": "pose_stream", "max_rate_deg_s": max_rate_deg_s, "timeout_s": timeout_s}, wait=False)
        return base

    def set_pose(self, joints: dict[int, float], done: bool = False) -> None:
        """Live J1-J6 targets (degrees). Call at least every timeout_s."""
        self._pose_seq = getattr(self, "_pose_seq", 0) + 1
        if self.dry_run:
            return
        self._write_atomic(POSE_FILE, {"seq": self._pose_seq, "joints": {str(j): float(v) for j, v in joints.items()},
                                       "done": bool(done)})

    def end_pose_stream(self) -> dict:
        """End the stream (arm_server takes the arm back to the start pose) and wait until it has."""
        self.set_pose({}, done=True)
        return {"result": "dry run"} if self.dry_run else self.wait_idle()

    def run(self, path: Path, mode: str = "path") -> dict:
        """Execute a planned Path on the arm.

        Inputs
            path : a Path from motion.plan_lift / plan_lower / plan_travel
            mode : "path" (default, continuous) or "moves" (fallback, slows at every 10 mm chunk)
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
