"""Shared helpers for pour/* scripts: locate arm_server files, send commands, read status.

All movement is through arm_cmd.json / arm_status.json (same contract as teleop/arm_cmd.py).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent              # pour/
LAMINAR = HERE.parent
TELEOP = LAMINAR / "teleop"
VENV_PY = LAMINAR / ".venv" / "Scripts" / "python.exe"
CMD_FILE = TELEOP / "arm_cmd.json"
STATUS_FILE = TELEOP / "arm_status.json"
STOP_FILE = TELEOP / "STOP"
LOG_DIR = HERE / "logs"

# follower motor-frame soft limits (deg) — same as arm_server.LIMITS, duplicated so pour/ can
# clamp without importing from teleop/
LIMITS = {
    1: (-145.0, 145.0),
    2: (0.0, 170.0),
    3: (0.0, 180.0),
    4: (-80.0, 90.0),
    5: (-90.0, 90.0),
    6: (-90.0, 90.0),
    7: (0.0, 270.0),  # gripper
}


def read_status():
    """Latest arm_status.json dict, or None if unreadable."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def status_fresh(max_age_s=2.0):
    if not STATUS_FILE.exists():
        return False
    return time.time() - STATUS_FILE.stat().st_mtime < max_age_s


def send_cmd(op, timeout=60.0, poll=0.05, **fields):
    """Write arm_cmd.json atomically and wait for arm_server to finish. Returns the final status dict.

    Mirrors teleop/arm_cmd.py behaviour: completion = status has matching id, busy=False,
    result != "running". Raises TimeoutError if the deadline passes."""
    cmd_id = time.time_ns()
    cmd = {"id": cmd_id, "op": op, **fields}
    tmp = CMD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd))
    for _ in range(50):
        try:
            os.replace(tmp, CMD_FILE)
            break
        except PermissionError:
            time.sleep(0.01)
    end = time.time() + timeout
    while time.time() < end:
        st = read_status()
        if st and st.get("id") == cmd_id and not st.get("busy") and st.get("result") != "running":
            return st
        if op == "rest" and st and "motors disabled" in str(st.get("result", "")):
            return st
        time.sleep(poll)
    raise TimeoutError(f"arm_server did not finish cmd {op} within {timeout}s")


def current_joints(status=None):
    """{1..6: deg} from arm_status.json 'command' (the latest issued target)."""
    st = status if status is not None else read_status()
    if st is None or "command" not in st:
        raise RuntimeError("no arm_status.json — is arm_server running?")
    return {m: float(st["command"][f"J{m}"]) for m in range(1, 7)}


def current_gripper_target(status=None):
    """Gripper target (deg from zero, 0 = closed) from status."""
    st = status if status is not None else read_status()
    if st is None:
        raise RuntimeError("no arm_status.json — is arm_server running?")
    return float(st.get("gripper", {}).get("target", 0.0))


def clamp_joints(joints):
    return {m: min(LIMITS[m][1], max(LIMITS[m][0], float(v))) for m, v in joints.items()}


def ensure_server(hold_grip=False, hold_torque=1.0, startup_timeout=15.0):
    """Start arm_server as a subprocess if its status file is stale, and wait until it's fresh.

    Returns the subprocess.Popen if we started one (so the caller can terminate it), else None.
    Does NOT clean up STOP_FILE — the caller might rely on it."""
    LOG_DIR.mkdir(exist_ok=True)
    if status_fresh():
        return None
    log = LOG_DIR / f"arm_server_{time.strftime('%Y%m%d_%H%M%S')}.log"
    args = [str(VENV_PY), "arm_server.py"]
    if hold_grip:
        args += ["--hold-grip", "--hold-torque", str(hold_torque)]
    STOP_FILE.unlink(missing_ok=True)
    proc = subprocess.Popen(args, cwd=str(TELEOP),
                            stdout=open(log, "w"), stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
    end = time.time() + startup_timeout
    while time.time() < end:
        if status_fresh():
            # also require a "ready" string in the result to make sure startup completed
            st = read_status()
            if st and "ready" in str(st.get("result", "")).lower():
                return proc
        if proc.poll() is not None:
            raise RuntimeError(f"arm_server exited during startup (code {proc.returncode}); see {log}")
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"arm_server did not report ready within {startup_timeout}s; see {log}")


def stop_server():
    """Ask arm_server to rest and exit via the STOP sentinel."""
    STOP_FILE.touch()
