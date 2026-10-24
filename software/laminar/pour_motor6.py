"""Camera-driven pour on motor 6 (wrist roll). Three functions:

    calibrate()     calibrate the side camera (interactive: click the columns, then fill column B by eye
                    to 25, 50, 75, 100 % of full, ENTER at each)
    pour_right()    pour into column A tilting right    } both first check the camera is calibrated:
    pour_left()     pour into column A tilting left     } not calibrated -> return without moving

A pour tilts J6 until liquid arrives, then steers the ANGLE from the live camera fill rate:

       fill (of target)   max fill rate   faster than that -> tilt back
       0 - 30 %           25 %/s          ("0-25 % in 1 s" is the limit)
       30 - 60 %          15 %/s
       60 - 80 %           8 %/s
       80 - 90 %           4 %/s
       90 - 100 %          2 %/s

and tilts back upright when: target reached (early, by fill rate x lag), liquid in the wrong
column, overfill, the camera loses the column, or no flow at max tilt.

Fill, fill rate and the wrong-column / overfill / camera checks come from Amogh's PourGuide
(sense/pour_guide.py); this file only turns them into motor-6 angles.

Real pours run through robotics/arm_server.py (it holds J1-J5 while this streams J6), so start it first:
    cd robotics && ../.venv/bin/python mac_run.py arm_server.py

    python -m laminar.pour_motor6 calibrate          # camera calibration: column B at 25/50/75/100 % full
    python -m laminar.pour_motor6 pour right         # pour into A; returns at once if not calibrated
    python -m laminar.pour_motor6 pour left
    python -m laminar.pour_motor6 home               # every joint to its calibrated zero, motors off
Simulated test (fake camera calibration, simulated camera and liquid):
    python -m laminar.pour_motor6 calibrate --sim                 # fake camera calibration
    python -m laminar.pour_motor6 pour right --sim                # simulated motor too
    python -m laminar.pour_motor6 pour left --sim --real-motor    # the REAL wrist pours (arm folded at rest)
    python -m laminar.pour_motor6 pour right --sim --scenario wrong_column   # or surge | dry | overfill | blocked
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .telemetry import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "sense"))

SENSE_DIR = REPO_ROOT / "sense"
DEFAULT_CALIB = SENSE_DIR / "level_calib.json"

# (fill fraction of target where the step starts, max fill fraction per second from there on)
RATE_SCHEDULE = [(0.0, 0.25), (0.3, 0.15), (0.6, 0.08), (0.8, 0.04), (0.9, 0.02)]


@dataclass
class PourConfig:
    target_frac: float = 0.9        # fill target: fraction of FULL with the percent calibration
                                    # (pour_right/left convert it), else fraction of the column box
    max_tilt_deg: float = 85.0      # never tilt further than this from the start angle
    search_deg_s: float = 15.0      # tilt speed while nothing is flowing yet
    kp_deg_s: float = 150.0         # deg/s of tilt per (fill/s) of rate error
    max_rate_deg_s: float = 30.0    # tilt speed limit while pouring
    too_fast_factor: float = 1.5    # rate above allowed x this: tilt back at full speed
    back_deg_s: float = 60.0        # final tilt back to upright
    lag_s: float = 0.3              # s from tilt-back until the stream stops (PourGuide's early stop)
    dry_s: float = 2.0              # at max tilt with no flow this long -> abort
    timeout_s: float = 60.0
    hz: float = 30.0
    overflow_margin: float = 0.05   # final fill above target + this = overfilled


@dataclass
class PourResult:
    ok: bool
    result: str                     # done | aborted | not_started
    reason: str
    column: str
    direction: str
    final_fill: float | None = None  # fraction of target
    final_ml: float | None = None
    duration_s: float = 0.0
    max_tilt_deg: float = 0.0
    log: list[dict] = field(default_factory=list)
    guide: dict | None = None        # PourGuide.summary()


def allowed_rate(fill: float) -> float:
    """Max fill fraction per second at this fill (fraction of target)."""
    rate = RATE_SCHEDULE[0][1]
    for start, r in RATE_SCHEDULE:
        if fill >= start:
            rate = r
    return rate


# ---------------------------------------------------------------- 1. calibrate (camera)

def calibrate(cam: str | int = "0", column: str = "B", columns: int = 2, empty: int = 8,
              marks: str = "25,50,75,100", capacity_ml: float = 4.5,
              calib_path: str | Path = DEFAULT_CALIB) -> list[str]:
    """Calibrate the side camera with Amogh's calibrate_level.py (interactive window + Terminal).

    Click the boxes for A..(columns) and the `empty` must-stay-empty columns, capture the empty
    reference, then fill `column` (B) by eye to each mark (% of full) and press ENTER at each;
    the other columns copy its table. Returns the problems left afterwards: empty list = calibrated.
    """
    cmd = [sys.executable, "calibrate_level.py", "--cam", str(cam), "--columns", str(columns),
           "--syringe", column, "--percent", "--marks", marks, "--capacity-ml", str(capacity_ml),
           "--empty", str(empty), "--out", str(calib_path)]
    subprocess.run(cmd, cwd=SENSE_DIR)
    problems = []
    for col in [chr(ord("A") + i) for i in range(columns)]:
        problems += [p for p in check_calibration(calib_path, col) if p not in problems]
    return problems


def _ref_path(calib_path: str | Path) -> Path | None:
    """The empty reference image, resolved next to the calibration file (calibrate_level.py saves a relative name)."""
    ref = json.loads(Path(calib_path).read_text()).get("ref_image")
    if not ref:
        return None
    p = Path(ref)
    return p if p.is_absolute() or p.exists() else Path(calib_path).parent / ref


def _load_reference(calib_path: str | Path, frame_shape=None):
    import cv2
    p = _ref_path(calib_path)
    ref = cv2.imread(str(p)) if p and p.exists() else None
    if ref is not None and frame_shape is not None and ref.shape != tuple(frame_shape):
        return None
    return ref


def check_calibration(calib_path: str | Path, column: str) -> list[str]:
    """Problems with level_calib.json for this column; empty list = calibrated."""
    p = Path(calib_path)
    if not p.exists():
        return [f"{p.name} not found (run: calibrate)"]
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        return [f"{p.name} is not valid JSON: {e}"]
    problems = []
    cols = data.get("columns", {})
    if column not in cols:
        problems.append(f"column {column} is not calibrated (have: {', '.join(cols) or 'none'})")
    elif len(cols[column].get("curve", [])) < 2:
        problems.append(f"column {column} has no syringe curve (px -> mL)")
    ref = _ref_path(p)
    if ref is None:
        problems.append("no empty reference image in the calibration")
    elif not ref.exists():
        problems.append(f"empty reference image {ref.name} is missing")
    return problems


def check_live(tracker, column: str, seconds: float = 1.0, clock=time) -> list[str]:
    """Fresh, trusted readings from the camera for this column."""
    end = clock.time() + seconds
    while clock.time() < end:
        clock.sleep(0.05)
    s = tracker.read(column)
    problems = []
    if clock.time() - s.ts > 0.5:
        problems.append(f"camera frames are stale ({clock.time() - s.ts:.1f} s old): is the camera running?")
    if s.conf < 0.5:
        problems.append(f"camera does not trust column {column} (conf {s.conf:.2f}): blocked, dark or moved?")
    return problems


# ---------------------------------------------------------------- 2/3. pour right / pour left

def pour_right(joint, tracker, column: str = "A", calib_path: str | Path | None = DEFAULT_CALIB,
               empty=None, cfg: PourConfig | None = None, **kw) -> PourResult:
    """Pour into `column` rolling motor 6 in the + direction. Returns without moving if the camera
    is not calibrated or not giving trusted readings."""
    return _checked_pour(joint, tracker, column, +1, calib_path, empty, cfg, **kw)


def pour_left(joint, tracker, column: str = "A", calib_path: str | Path | None = DEFAULT_CALIB,
              empty=None, cfg: PourConfig | None = None, **kw) -> PourResult:
    """Pour into `column` rolling motor 6 in the - direction. Same checks as pour_right."""
    return _checked_pour(joint, tracker, column, -1, calib_path, empty, cfg, **kw)


def target_box_frac(calib: dict, column: str, target_full: float) -> float | None:
    """Turn a target in fraction-of-FULL (from the percent calibration) into the fraction of the
    column box PourGuide works in. None if this calibration has no fill_curve (mL calibration)."""
    curve = calib.get("fill_curve", {}).get(column)
    if not curve or len(curve) < 2:
        return None
    pxs, fracs = zip(*sorted(curve))
    box_h = float(calib["columns"][column]["roi"][3])
    if target_full <= fracs[-1]:
        px = float(np.interp(target_full, fracs, pxs))
    else:                           # above the top mark: extend the last segment
        slope = (pxs[-1] - pxs[-2]) / (fracs[-1] - fracs[-2])
        px = pxs[-1] + slope * (target_full - fracs[-1])
    return max(0.1, min(1.0, px / box_h))


def _checked_pour(joint, tracker, column, direction, calib_path, empty, cfg, **kw) -> PourResult:
    import dataclasses
    side = "right" if direction > 0 else "left"
    cfg = cfg or PourConfig()
    if calib_path is not None:      # None = simulator, no calibration file
        problems = check_calibration(calib_path, column)
        if problems:
            return PourResult(False, "not_started", "camera not calibrated: " + "; ".join(problems), column, side)
        calib = json.loads(Path(calib_path).read_text())
        if empty is None:
            empty = calib.get("empty", [])
        box = target_box_frac(calib, column, cfg.target_frac)
        if box is not None:         # percent calibration: target is a fraction of full
            cfg = dataclasses.replace(cfg, target_frac=box)
    problems = check_live(tracker, column, clock=kw.get("clock", time))
    if problems:
        return PourResult(False, "not_started", "camera not ready: " + "; ".join(problems), column, side)
    return _pour(joint, tracker, column, direction, empty or (), cfg, **kw)


def _pour(joint, tracker, column: str, direction: int, empty=(), cfg: PourConfig | None = None,
          clock=time, log_path: str | None = None, verbose: bool = True, on_tick=None) -> PourResult:
    """on_tick(rec) is called every loop with the live numbers (e.g. a camera window); it may raise
    KeyboardInterrupt to stop the pour."""
    from pour_guide import PourGuide

    cfg = cfg or PourConfig()
    side = "right" if direction > 0 else "left"
    base = joint.position()
    if base is None:
        return PourResult(False, "not_started", "cannot read motor 6 position", column, side)
    # room to the +-90 deg wrist limit in the pour direction (negative = already past it)
    limit = joint.hi if direction > 0 else joint.lo
    room = math.degrees((limit - base) * direction) - 2.0
    max_tilt = min(cfg.max_tilt_deg, room)
    if max_tilt < 20:
        where = getattr(joint, "base_deg", None)
        at = f" (wrist roll at {where:+.0f} deg, limit +-90)" if where is not None else ""
        why = (f"wrist roll is already past its limit on the {side}{at}" if room < 0
               else f"only {room:.0f} deg of room to the wrist limit on the {side}{at}")
        other = "left" if direction > 0 else "right"
        return PourResult(False, "not_started", f"{why}: roll it back toward 0 first, or pour {other}", column, side)

    guide = PourGuide(tracker, column, target_frac=cfg.target_frac, lag_s=cfg.lag_s,
                      no_rise_s=0, empty=empty, log_path=log_path)
    guide.begin()
    dt = 1.0 / cfg.hz
    tilt = 0.0
    t0 = last_flow = clock.time()
    flowing = False
    log: list[dict] = []
    last_action = None
    cmd = None
    result, reason = "aborted", ""
    try:
        while True:
            now = clock.time()
            cmd = guide.update()
            if cmd.stop:
                result, reason = "done", cmd.reason
                break
            if cmd.abort:
                reason = cmd.reason
                break
            if now - t0 > cfg.timeout_s:
                reason = f"pour timed out after {cfg.timeout_s:.0f} s"
                break

            allowed = allowed_rate(cmd.fill)
            if cmd.rate > 0.01:
                flowing, last_flow = True, now
            if cmd.phase == "hold":
                action, rate_deg = "hold", 0.0                 # camera unsure: keep the angle
            elif cmd.rate > allowed * cfg.too_fast_factor:
                action, rate_deg = "too_fast", -cfg.max_rate_deg_s
            elif not flowing:
                action, rate_deg = "tilt_more", cfg.search_deg_s
            else:
                rate_deg = max(-cfg.max_rate_deg_s, min(cfg.max_rate_deg_s, cfg.kp_deg_s * (allowed - cmd.rate)))
                action = "slow_down" if rate_deg < -1 else ("tilt_more" if rate_deg > 1 else "hold")
            tilt = max(0.0, min(max_tilt, tilt + rate_deg * dt))
            if tilt >= max_tilt - 1e-6 and now - last_flow > cfg.dry_s:
                reason = f"no flow at max tilt ({max_tilt:.0f} deg): vial empty or missing the column?"
                break
            joint.move_to(base + direction * math.radians(tilt), vlim=math.radians(cfg.max_rate_deg_s * 1.5))

            rec = {"t": round(now - t0, 3), "tilt_deg": round(tilt, 2), "fill": cmd.fill, "rate": cmd.rate,
                   "allowed": allowed, "ml": cmd.ml, "phase": cmd.phase, "action": action}
            log.append(rec)
            if verbose and action != last_action:
                print(f"  t={rec['t']:5.2f}s  fill {cmd.fill:6.1%}  rate {cmd.rate:5.1%}/s "
                      f"(max {allowed:.0%}/s)  tilt {tilt:5.1f} deg  -> {action}")
                last_action = action
            if on_tick:
                on_tick({**rec, "side": side, "target_px": guide.target_px})
            clock.sleep(dt)
    except KeyboardInterrupt:
        reason = "stopped by operator"
    finally:
        _tilt_back(joint, base, tilt, cfg, clock)

    # read the level again once what was still in the air has landed
    end = clock.time() + cfg.lag_s + 0.5
    while clock.time() < end:
        clock.sleep(0.05)
    s = tracker.read(column)
    final_fill = s.px / guide.target_px if s.conf >= 0.5 else (cmd.fill if cmd else None)
    final_ml = s.ml if s.conf >= 0.5 else (cmd.ml if cmd else None)
    if result == "done" and final_fill is not None and final_fill > 1.0 + cfg.overflow_margin:
        result, reason = "aborted", f"overfilled after stop: {final_fill:.0%} of target (lag longer than {cfg.lag_s:.2f} s?)"

    if verbose:
        print(f"  {'DONE' if result == 'done' else 'STOPPED'}: {reason}  (settled at {final_fill:.0%} of target)")
    if on_tick:
        try:
            on_tick({"final": True, "side": side, "result": result, "reason": reason, "fill": final_fill,
                     "target_px": guide.target_px})
        except KeyboardInterrupt:
            pass
    return PourResult(result == "done", result, reason, column, side,
                      final_fill=round(final_fill, 4) if final_fill is not None else None,
                      final_ml=round(final_ml, 3) if final_ml is not None else None,
                      duration_s=round(clock.time() - t0, 2),
                      max_tilt_deg=round(max((r["tilt_deg"] for r in log), default=0.0), 1),
                      log=log, guide=guide.summary())


def _tilt_back(joint, base: float, tilt_deg: float, cfg: PourConfig, clock) -> None:
    """Back upright at a controlled speed; every pour ends this way."""
    joint.move_to(base, vlim=math.radians(cfg.back_deg_s))
    end = clock.time() + tilt_deg / cfg.back_deg_s + 1.0
    while clock.time() < end:
        p = joint.position()
        if p is not None and abs(p - base) < 0.02:
            break
        clock.sleep(0.02)


# ---------------------------------------------------------------- arm_server backend (real pours)

class ServerTiltJoint:
    """Motor 6 through robotics/arm_server.py's tilt_stream: arm_server holds J1-J5 (and the gripper)
    while this streams only the wrist roll. Positions are radians of offset from where the roll was
    when the stream started.

    arm_server ends the stream (and tilts back upright) if updates stop for 0.5 s, so the stream only
    starts on the first position()/move_to() (when the pour begins, not while a prompt waits), and a
    heartbeat thread re-sends the current angle every 0.1 s until close().
    """

    def __init__(self, joint: int = 6, soft_limit_deg: float = 90.0, max_rate_deg_s: float = 120.0):
        import threading
        sys.path.insert(0, str(REPO_ROOT / "robotics"))
        from arm_client import ArmClient
        self.client = ArmClient()
        try:
            self.client.current_joints()     # also refuses a stale status file left by an exited arm_server
        except RuntimeError as e:
            raise RuntimeError(f"{e}  Start it with `python mac_run.py arm_server.py` in robotics/") from None
        self.joint, self.soft_limit_deg, self.max_rate_deg_s = joint, soft_limit_deg, max_rate_deg_s
        self.base_deg: float | None = None
        self.lo, self.hi = -math.radians(soft_limit_deg), math.radians(soft_limit_deg)   # until started
        self._pos = self._target = 0.0
        self._v = math.radians(30.0)
        self._t = time.monotonic()
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def _start(self) -> None:
        import threading
        self.base_deg = self.client.start_tilt_stream(self.joint, self.max_rate_deg_s)
        self.lo = math.radians(-self.soft_limit_deg - self.base_deg)
        self.hi = math.radians(self.soft_limit_deg - self.base_deg)
        self._t = time.monotonic()
        self._running = True
        self._thread = threading.Thread(target=self._heartbeat, name="tilt-heartbeat", daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while self._running:
            with self._lock:
                if self._running:
                    self._update()
            time.sleep(0.1)

    def _update(self) -> None:
        now = time.monotonic()
        step = self._v * min(now - self._t, 0.05)
        self._t = now
        self._pos += max(-step, min(step, self._target - self._pos))
        self.client.set_tilt(math.degrees(self._pos))

    def position(self) -> float:
        with self._lock:
            if self.base_deg is None:
                self._start()
            self._update()
            return self._pos

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        with self._lock:
            if self.base_deg is None:
                self._start()
            self._target = max(self.lo, min(self.hi, pos))
            if vlim:
                self._v = vlim
            self._update()

    def close(self) -> None:
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.base_deg is not None:
            self.client.end_tilt_stream()


# ---------------------------------------------------------------- simulator (no camera, no arm)

class SimClock:
    """Fake time. sleep() advances it and steps the simulated physics."""

    def __init__(self, realtime: float = 0.0):
        self.t = 1_000_000.0
        self.realtime = realtime
        self.on_step = []

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        for f in self.on_step:
            f(s)
        if self.realtime:
            time.sleep(s * self.realtime)


class RealClock:
    """Real time, same interface as SimClock: sleep() also steps the simulated liquid by the time that
    really passed. Lets a REAL motor 6 pour into simulated liquid."""

    def __init__(self):
        self.on_step = []
        self._last = time.monotonic()

    @property
    def t(self) -> float:
        return time.time()

    def time(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)
        now = time.monotonic()
        dt, self._last = min(now - self._last, 0.1), now
        for f in self.on_step:
            f(dt)


class SimJoint:
    """Motor 6 that follows move_to() at the requested speed in SimClock time."""

    def __init__(self, clock: SimClock, start: float = 0.0):
        self.lo, self.hi = -math.pi / 2, math.pi / 2
        self.pos = self.target = start
        self.v = 1.0
        clock.on_step.append(self._step)

    def _step(self, dt: float) -> None:
        d = self.target - self.pos
        self.pos += max(-self.v * dt, min(self.v * dt, d))

    def position(self) -> float:
        return self.pos

    def move_to(self, pos: float, vlim: float | None = None) -> None:
        self.target, self.v = pos, vlim or self.v


class SimTracker:
    """Columns A..D plus empty X1, X2. Liquid flows into A once the tilt passes onset_deg and keeps
    arriving for a moment after tilt-back (stream_tau_s).

    scenario: normal | wrong_column (stream lands in C after 1 mL) | surge (stream triples for 0.4 s) |
              dry (vial empty) | overfill (a sudden 1 mL glug near the end) | blocked (camera loses A)
    """

    BOX_PX, ML_PER_PX = 200.0, 0.02

    def __init__(self, clock: SimClock, joint: SimJoint, direction: int, scenario: str = "normal",
                 onset_deg: float = 40.0, k_ml_s_per_deg: float = 0.05, stream_tau_s: float = 0.2):
        from level import Column, LevelSample
        self._Sample = LevelSample
        self.clock, self.joint, self.direction = clock, joint, direction
        self.base = joint.position()
        self.scenario = scenario
        self.onset, self.k, self.tau = onset_deg, k_ml_s_per_deg, stream_tau_s
        names = ["A", "B", "C", "D", "X1", "X2"]
        curve = [(0.0, 0.0), (self.BOX_PX, self.BOX_PX * self.ML_PER_PX)]
        self.columns = {n: Column(n, (0, 0, 10, int(self.BOX_PX)), curve) for n in names}
        self.ml = {n: 0.0 for n in names}
        self.poured = 0.0
        self.flow = 0.0                # stream actually landing, mL/s
        self._flow_t = None
        self._glugged = False
        clock.on_step.append(self._step)

    def _step(self, dt: float) -> None:
        tilt = math.degrees((self.joint.position() - self.base) * self.direction)
        want = 0.0 if self.scenario == "dry" else self.k * max(0.0, tilt - self.onset)
        self.flow += (want - self.flow) * min(1.0, dt / self.tau)
        flow = self.flow
        if flow > 0.01 and self._flow_t is None:
            self._flow_t = self.clock.t
        if self.scenario == "surge" and self._flow_t and 1.0 < self.clock.t - self._flow_t < 1.4:
            flow *= 3
        into = "C" if self.scenario == "wrong_column" and self.poured > 1.0 else "A"
        self.ml[into] += flow * dt
        self.poured += flow * dt
        if self.scenario == "overfill" and not self._glugged and self.ml["A"] > 0.8 * self.BOX_PX * self.ML_PER_PX:
            self.ml["A"] += 1.0
            self._glugged = True

    def read(self, name):
        ml = self.ml[name]
        conf = 0.1 if (self.scenario == "blocked" and name == "A" and self._flow_t) else 1.0
        return self._Sample(ml, ml / self.ML_PER_PX, conf, self.clock.t, "blue" if ml > 0.05 else "none")

    def flow(self, name, window_s=0.3):
        return 0.0


SIM_CALIB = REPO_ROOT / "software" / "level_calib_sim.json"   # fake camera calibration, never used for real


def fake_calibrate(path: str | Path = SIM_CALIB) -> Path:
    """Write a fake camera calibration matching the simulated camera (columns A-D, empty X1, X2)."""
    p = Path(path)
    box, ml = int(SimTracker.BOX_PX), SimTracker.BOX_PX * SimTracker.ML_PER_PX
    data = {"columns": {c: {"roi": [0, 0, 10, box], "curve": [[0, 0], [box, ml]]} for c in "ABCD"},
            "fill_curve": {c: [[0, 0], [box / 4, 0.25], [box / 2, 0.5], [3 * box / 4, 0.75], [box, 1.0]]
                           for c in "ABCD"},
            "capacity_ml": ml, "empty": ["X1", "X2"], "ref_image": "level_ref_sim.png", "fake": True}
    (p.parent / "level_ref_sim.png").write_bytes(b"simulated")
    p.write_text(json.dumps(data, indent=2))
    return p


def simulate(direction: int, scenario: str = "normal", target_frac: float = 0.9, realtime: float = 0.0,
             calib_path: str | Path | None = None, verbose: bool = True) -> PourResult:
    """A whole pour into A, simulated (camera, liquid and motor). calib_path=None skips the calibration check."""
    import pour_guide
    clock = SimClock(realtime)
    joint = SimJoint(clock)
    tracker = SimTracker(clock, joint, direction, scenario=scenario)
    real_time, pour_guide.time = pour_guide.time, clock   # PourGuide reads time through its `time` module
    try:
        fn = pour_right if direction > 0 else pour_left
        return fn(joint, tracker, "A", calib_path=calib_path, empty=("X1", "X2"),
                  cfg=PourConfig(target_frac=target_frac), clock=clock, verbose=verbose)
    finally:
        pour_guide.time = real_time


def motor_pour(joint, direction: int, scenario: str = "normal", target_frac: float = 0.9,
               calib_path: str | Path | None = None, verbose: bool = True) -> PourResult:
    """A REAL (or fake) motor 6 pours into simulated liquid, in real time: the liquid responds to the
    motor's measured angle. Shows exactly how the wrist moves in a pour, with no camera or liquid."""
    clock = RealClock()
    tracker = SimTracker(clock, joint, direction, scenario=scenario)
    fn = pour_right if direction > 0 else pour_left
    return fn(joint, tracker, "A", calib_path=calib_path, empty=("X1", "X2"),
              cfg=PourConfig(target_frac=target_frac), clock=clock, verbose=verbose)


# ---------------------------------------------------------------- CLI

def home(speed_deg_s: float = 15.0, timeout_s: float = 90.0) -> str:
    """Every joint back to its calibrated zero (the resting pose), then the motors are switched off.

    arm_server running -> its `rest` command (it eases to zero at 15 deg/s, disables, exits).
    Not running        -> robotics/rest_arm.py through mac_run.py.
    """
    robotics = REPO_ROOT / "robotics"
    sys.path.insert(0, str(robotics))
    from arm_client import ArmClient
    client = ArmClient()
    try:
        cmd_id = client._send({"op": "rest"}, wait=False)["id"]
    except RuntimeError:      # arm_server did not pick it up: it is not running
        print("arm_server not running: using rest_arm.py")
        r = subprocess.run([sys.executable, "mac_run.py", "rest_arm.py", "--speed", str(speed_deg_s)], cwd=robotics)
        return "at rest, motors disabled" if r.returncode == 0 else f"rest_arm.py failed (exit {r.returncode})"
    end = time.time() + timeout_s
    while time.time() < end:
        st = client.status() or {}
        if "motors disabled" in str(st.get("result")) or (st.get("id") == cmd_id and "rest" in str(st.get("op"))
                                                           and "disabled" in str(st.get("result"))):
            return str(st.get("result"))
        time.sleep(0.2)
    return "timed out waiting for arm_server to reach rest"


class PourView:
    """Live camera window during a real pour: column boxes, level line, target line, and the pour's
    numbers. Press q in the window to stop the pour (the wrist tilts back)."""

    WINDOW = "pour (q = stop)"

    def __init__(self, cam, tracker, column: str, empty=()):
        import cv2
        self.cv2, self.cam, self.tracker, self.column, self.empty = cv2, cam, tracker, column, empty

    def __call__(self, rec: dict) -> None:
        from level import draw_overlay
        cv2 = self.cv2
        frame, _ = self.cam.read()
        if frame is None:
            return
        img = draw_overlay(frame.copy(), self.tracker.columns, self.tracker, empty=self.empty)
        x, y, w, h = self.tracker.columns[self.column].roi
        ty = int(y + h - rec["target_px"])
        cv2.line(img, (x - 8, ty), (x + w + 8, ty), (255, 0, 255), 1)
        cv2.putText(img, "target", (x + w + 10, ty + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 255), 1)
        if rec.get("final"):
            ok = rec["result"] == "done"
            fill = f"{rec['fill']:.0%}" if rec.get("fill") is not None else "?"
            good, bad = (80, 220, 80), (80, 80, 255)
            lines = [(f"POUR {rec['side'].upper()} {'DONE' if ok else 'STOPPED'}  fill {fill} of target",
                      good if ok else bad), (rec["reason"][:70], good if ok else bad)]
        else:
            white, warn = (240, 240, 240), (80, 160, 255)
            fast = rec["rate"] > rec["allowed"] * 1.15
            lines = [(f"POUR {rec['side'].upper()}  t={rec['t']:.1f}s  tilt {rec['tilt_deg']:.0f} deg  -> {rec['action']}",
                      warn if rec["action"] in ("too_fast", "slow_down") else white),
                     (f"fill {rec['fill']:.0%} of target   rate {rec['rate']:.0%}/s (max {rec['allowed']:.0%}/s)",
                      warn if fast else white)]
        cv2.rectangle(img, (0, 0), (img.shape[1], 12 + 24 * len(lines)), (30, 30, 30), -1)   # dark banner
        for i, (text, color) in enumerate(lines):
            cv2.putText(img, text, (10, 26 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        cv2.imshow(self.WINDOW, img)
        key = cv2.waitKey(2000 if rec.get("final") else 1) & 0xFF
        if key == ord("q") and not rec.get("final"):
            raise KeyboardInterrupt

    def close(self) -> None:
        self.cv2.destroyAllWindows()
        self.cv2.waitKey(1)


def arm_server_running() -> bool:
    """True if robotics/arm_server.py is up (its status file is fresh)."""
    sys.path.insert(0, str(REPO_ROOT / "robotics"))
    from arm_client import ArmClient
    try:
        ArmClient().current_joints()
        return True
    except (RuntimeError, OSError):
        return False


def _open_joint(backend: str):
    if backend == "server":
        return ServerTiltJoint(6)   # needs `python mac_run.py arm_server.py` running in robotics/
    if arm_server_running():
        raise RuntimeError("arm_server is running and owns the CAN adapter: use --backend server "
                           "(or stop arm_server first)")
    from .motor6 import RobStrideJoint
    print("  direct backend: only J6 is enabled, J1-J5 are LIMP. Use only with the arm folded at rest.")
    j = RobStrideJoint(6)
    try:
        j.connect()
    except Exception:
        j.close()
        raise
    return j


def main() -> int:
    ap = argparse.ArgumentParser(description="calibrate = camera calibration (syringe in B); "
                                 "pour right|left = pour into A, returns at once if the camera is not calibrated")
    ap.add_argument("command", choices=["calibrate", "pour", "home"])
    ap.add_argument("side", nargs="?", choices=["right", "left"], help="pour: which way to tilt")
    ap.add_argument("--cam", default="0", help="side camera index or /dev path (0 = the USB camera here)")
    ap.add_argument("--col", default=None, help="column (default: B for calibrate, A for pour)")
    ap.add_argument("--calib", default=str(DEFAULT_CALIB), help="camera calibration file")
    ap.add_argument("--marks", default="25,50,75,100", help="calibrate: fill marks, %% of full")
    ap.add_argument("--capacity-ml", type=float, default=4.5, help="calibrate: mL in a full column")
    ap.add_argument("--empty", type=int, default=8, help="calibrate: how many must-stay-empty columns to click")
    ap.add_argument("--target", type=float, default=0.9, help="pour: fill target, fraction of FULL (0.9 = 90%%)")
    ap.add_argument("--max-tilt", type=float, default=85.0, help="pour: deg from the start angle")
    ap.add_argument("--backend", choices=["server", "direct"], default=None,
                    help="server = arm_server holds J1-J5 and streams J6 (use this with the arm raised); "
                         "direct = only J6 enabled, J1-J5 limp (arm folded at rest only). "
                         "Default: server for real pours; for --sim --real-motor, server if arm_server is running, else direct")
    ap.add_argument("--log", default=None, help="pour: PourGuide .jsonl log")
    ap.add_argument("--sim", action="store_true",
                    help="simulated test: calibrate writes a fake camera calibration; pour uses simulated camera "
                         "and liquid (and a simulated motor unless --real-motor)")
    ap.add_argument("--real-motor", action="store_true", help="with --sim: the real J6 does the pour")
    ap.add_argument("--scenario", default="normal",
                    choices=["normal", "wrong_column", "surge", "dry", "overfill", "blocked"])
    ap.add_argument("--yes", action="store_true", help="skip the safety prompt")
    ap.add_argument("--no-view", action="store_true", help="pour: no live camera window")
    a = ap.parse_args()

    if a.command == "home":
        if not a.yes and input("HOME: every joint eases back to its calibrated zero (resting pose), then the motors "
                               "switch off. The wrist rolls upright and the base turns back, so take the vial OUT "
                               "of the gripper first. Area clear? [y/N] ").lower() != "y":
            return 0
        result = home()
        print(result)
        return 0 if "disabled" in result else 1

    if a.command == "calibrate":
        if a.sim:
            print(f"fake camera calibration saved to {fake_calibrate().name} (simulated test only)")
            return 0
        col = a.col or "B"
        print(f"camera calibration: click the columns, then fill column {col} to {a.marks} % of full")
        problems = calibrate(a.cam, col, empty=a.empty, marks=a.marks, capacity_ml=a.capacity_ml,
                             calib_path=a.calib)
        for p in problems:
            print(f"  NOT CALIBRATED: {p}")
        print("camera calibrated." if not problems else "camera calibration incomplete.")
        return 0 if not problems else 1

    if not a.side:
        ap.error("pour needs a side: right or left")
    direction = 1 if a.side == "right" else -1
    col = a.col or "A"
    fn = pour_right if direction > 0 else pour_left

    if a.sim:
        problems = check_calibration(SIM_CALIB, "A")
        if problems:
            print("NOT CALIBRATED (simulated test): " + "; ".join(problems))
            print("run first:  python -m laminar.pour_motor6 calibrate --sim")
            return 1
        if not a.real_motor:
            print(f"simulated pour {a.side} into A (simulated motor), scenario {a.scenario}")
            r = simulate(direction, a.scenario, a.target, realtime=0.3, calib_path=SIM_CALIB)
            return 0 if r.ok else 1
        backend = a.backend or ("server" if arm_server_running() else "direct")
        try:
            joint = _open_joint(backend)
        except Exception as e:
            print(f"could not start motor 6 ({backend}): {e}")
            return 1
        try:
            if not a.yes and input(f"REAL wrist pours {a.side} into SIMULATED liquid ({a.scenario}), tilting up to "
                                   f"{a.max_tilt:.0f} deg. {'Arm folded at rest, ' if backend == 'direct' else ''}"
                                   "area and cables clear? [y/N] ").lower() != "y":
                return 0
            r = motor_pour(joint, direction, a.scenario, a.target, calib_path=SIM_CALIB)
            print(f"result: {r.result} - {r.reason}")
            return 0 if r.ok else 1
        finally:
            joint.close()
            print("motor 6 released")

    # not calibrated -> return before touching the camera or the arm
    problems = check_calibration(a.calib, col)
    if problems:
        print("NOT CALIBRATED: " + "; ".join(problems))
        print("run first:  python -m laminar.pour_motor6 calibrate")
        return 1

    from cameras import LatestFrame, open_camera
    from level import LevelTracker, load_columns

    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    tracker = LevelTracker(cam, load_columns(a.calib))
    frame, _ = cam.read()
    t_wait = time.time() + 3
    while frame is None and time.time() < t_wait:
        time.sleep(0.05)
        frame, _ = cam.read()
    tracker.ref = _load_reference(a.calib, frame.shape if frame is not None else None)
    if tracker.ref is None:
        print("NOT CALIBRATED: the empty reference image does not match this camera. Run: calibrate")
        cam.stop()
        return 1
    tracker.start()

    backend = a.backend or "server"
    try:
        joint = _open_joint(backend)
    except Exception as e:
        print(f"could not start motor 6 ({backend}): {e}")
        tracker.stop()
        cam.stop()
        return 1
    view = None
    try:
        if not a.yes and input(f"pour {a.side} into column {col}. Vial in the gripper, spout over {col}, "
                               "area clear? [y/N] ").lower() != "y":
            return 0
        empty = json.loads(Path(a.calib).read_text()).get("empty", [])
        view = None if a.no_view else PourView(cam, tracker, col, empty)
        if view:
            print("camera window open: press q in it to stop the pour")
        r = fn(joint, tracker, col, calib_path=a.calib,
               cfg=PourConfig(target_frac=a.target, max_tilt_deg=a.max_tilt), log_path=a.log, on_tick=view)
        if r.result == "not_started":
            print(f"NOT STARTED: {r.reason}")
        print("pour:", json.dumps({k: v for k, v in r.__dict__.items() if k not in ("log", "guide")}, indent=2))
        return 0 if r.ok else 1
    finally:
        joint.close()
        if view:
            view.close()
        tracker.stop()
        cam.stop()
        print("motor 6 released, camera closed")


if __name__ == "__main__":
    raise SystemExit(main())
