"""Closed-loop pour: camera JSON frames in, joint-6 tilt out.

The side-cam OpenCV process (Sense) sends one PourFrame per frame, 30 Hz, as JSON over UDP
on localhost. PourController turns each frame into a tilt for the pour joint:

  1. pouring too fast, or close to the target  -> slow down (tilt back a little)
  2. tilted but nothing is coming out          -> tilt more; abort if still dry at max tilt
  3. wrong colour liquid in the tube           -> abort and tilt back
  plus: target reached -> stop; camera stale / low confidence -> hold, then abort.

run_pour() is the 30 Hz loop that ties a frame source, the controller and Arm.stream_q together.
It is the same loop for the fakes and the real arm. No network calls, no LLM.

PourFrame JSON (one object per frame):
    {"ts": 1761270000.123, "seq": 1042, "ml": 1.42, "flow_ml_s": 0.85, "pouring": true,
     "color": "blue", "color_conf": 0.93, "conf": 0.97, "level_px": 236}
"""
from __future__ import annotations

import json
import math
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum

import numpy as np

from . import telemetry
from .arm import Arm

NO_COLOR = ("none", "unknown")


@dataclass
class PourFrame:
    ts: float                 # time.time() when the frame was captured
    ml: float                 # volume in the tube since zero, mL
    flow_ml_s: float = 0.0    # smoothed d(ml)/dt, mL/s
    pouring: bool = False     # liquid visibly entering the tube
    color: str = "none"       # dominant liquid colour seen: "blue", "red", "none", "unknown"
    color_conf: float = 0.0   # 0..1
    conf: float = 1.0         # 0..1 overall; drops if the crop is dark, occluded or the tube is missing
    seq: int = 0              # frame counter, for spotting dropped frames
    level_px: int | None = None

    @classmethod
    def from_json(cls, data: str | bytes | dict) -> PourFrame:
        d = json.loads(data) if isinstance(data, (str, bytes)) else dict(data)
        for k in ("ts", "ml"):
            if not isinstance(d.get(k), (int, float)):
                raise ValueError(f"PourFrame needs a number for '{k}', got {d.get(k)!r}")
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class PourRecipe:
    max_tilt: float = 2.2          # rad beyond station_pour, never tilt further
    search_rate: float = 0.6       # rad/s while nothing is flowing
    kp: float = 0.8                # rad/s of tilt per mL/s of flow error
    max_rate: float = 1.0          # rad/s tilt rate limit
    flow_setpoint: float = 1.0     # mL/s in the main phase
    slow_setpoint: float = 0.3     # mL/s near the target
    slow_zone_ml: float = 0.6      # start slowing when this much is left
    surge_factor: float = 2.5      # flow above setpoint x this = surge, back off hard
    lag_s: float = 0.25            # s from tilt-back until flow stops; cut-off = remaining <= flow x lag
    min_flow: float = 0.1          # below this counts as not pouring
    dry_timeout_s: float = 1.5     # dry at max tilt this long -> abort
    wrong_color_frames: int = 3    # consecutive frames (~100 ms) before calling it the wrong liquid
    min_conf: float = 0.5
    stale_s: float = 0.15          # frame older than this is not trusted
    blind_timeout_s: float = 0.3   # no trusted frame this long -> abort
    max_time_s: float = 30.0
    back_rate: float = 3.0         # rad/s for the final tilt-back


# Placeholders until Avery tunes recipe.yaml from the teleop demo pours.
RECIPES = {
    "blue": PourRecipe(),
    "red": PourRecipe(flow_setpoint=0.6, slow_setpoint=0.25, lag_s=0.5, kp=0.6),
}


class PourState(str, Enum):
    SEARCHING = "searching"   # tilting until flow starts
    POURING = "pouring"
    SLOWING = "slowing"       # inside the slow zone
    DONE = "done"
    ABORTED = "aborted"


@dataclass
class PourCommand:
    tilt: float      # rad to add to the pour joint of station_pour
    state: PourState
    action: str      # tilt_more | slow_down | hold | stop | abort
    reason: str


class PourController:
    def __init__(self, target_ml: float, expected_color: str, recipe: PourRecipe | None = None):
        self.target_ml = target_ml
        self.expected_color = expected_color
        self.r = recipe or RECIPES.get(expected_color, PourRecipe())
        self.state = PourState.SEARCHING
        self.tilt = 0.0
        self.reason = ""
        self.elapsed = 0.0
        self.last_frame: PourFrame | None = None
        self._dry_s = 0.0
        self._blind_s = 0.0
        self._wrong = 0

    @property
    def active(self) -> bool:
        return self.state not in (PourState.DONE, PourState.ABORTED)

    def _end(self, state: PourState, action: str, reason: str) -> PourCommand:
        self.state, self.reason = state, reason
        return PourCommand(self.tilt, state, action, reason)

    def step(self, frame: PourFrame | None, now: float, dt: float) -> PourCommand:
        r = self.r
        if not self.active:
            return PourCommand(self.tilt, self.state, "hold", self.reason)
        self.elapsed += dt
        if self.elapsed > r.max_time_s:
            return self._end(PourState.ABORTED, "abort", f"pour timed out after {r.max_time_s:.0f} s")

        # camera trust: stale or low-confidence frames -> hold the tilt, abort if it lasts
        if frame is None or now - frame.ts > r.stale_s or frame.conf < r.min_conf:
            self._blind_s += dt
            why = "no frame" if frame is None else ("stale frame" if now - frame.ts > r.stale_s
                                                    else f"low camera confidence {frame.conf:.2f}")
            if self._blind_s > r.blind_timeout_s:
                return self._end(PourState.ABORTED, "abort", f"camera unusable for {self._blind_s:.2f} s ({why})")
            return PourCommand(self.tilt, self.state, "hold", why)
        self._blind_s = 0.0
        self.last_frame = frame

        # 3. wrong liquid
        if frame.color not in NO_COLOR and frame.color != self.expected_color and frame.color_conf >= 0.7:
            self._wrong += 1
            if self._wrong >= r.wrong_color_frames:
                return self._end(PourState.ABORTED, "abort",
                                 f"wrong liquid: camera sees {frame.color}, expected {self.expected_color}")
        else:
            self._wrong = 0

        remaining = self.target_ml - frame.ml
        flow = frame.flow_ml_s
        if remaining <= max(flow, 0.0) * r.lag_s:
            inflight = max(flow, 0.0) * r.lag_s
            return self._end(PourState.DONE, "stop", f"cut-off at {frame.ml:.2f} mL + ~{inflight:.2f} mL in flight "
                                                     f"(target {self.target_ml} mL)")

        # 2. not pouring while tilted -> tilt more
        if not frame.pouring or flow < r.min_flow:
            rate = r.search_rate
            if self.state == PourState.SEARCHING:
                action, reason = "tilt_more", "searching for flow onset"
            else:
                action, reason = "tilt_more", "flow stopped while pouring"
            if self.tilt >= r.max_tilt - 1e-6:
                self._dry_s += dt
                if self._dry_s > r.dry_timeout_s:
                    return self._end(PourState.ABORTED, "abort",
                                     "no flow at max tilt (vial empty or blocked?)")
                action, reason = "hold", "at max tilt, still no flow"
        else:
            # 1. pouring: track the flow setpoint, slower near the target
            self._dry_s = 0.0
            near = remaining < r.slow_zone_ml
            self.state = PourState.SLOWING if near else PourState.POURING
            setpoint = r.slow_setpoint if near else r.flow_setpoint
            if flow > setpoint * r.surge_factor:
                rate, action, reason = -2 * r.max_rate, "slow_down", f"surge: {flow:.2f} mL/s"
            else:
                rate = float(np.clip(r.kp * (setpoint - flow), -r.max_rate, r.max_rate))
                if flow > setpoint * 1.15:
                    action = "slow_down"
                    reason = f"near target, {remaining:.2f} mL left" if near else f"too fast: {flow:.2f} > {setpoint} mL/s"
                elif flow < setpoint * 0.85:
                    action, reason = "tilt_more", f"too slow: {flow:.2f} < {setpoint} mL/s"
                else:
                    action, reason = "hold", f"on target flow {flow:.2f} mL/s"
        self.tilt = float(np.clip(self.tilt + rate * dt, 0.0, r.max_tilt))
        return PourCommand(self.tilt, self.state, action, reason)


def run_pour(arm: Arm, controller: PourController, get_frame: Callable[[], PourFrame | None],
             base_q: np.ndarray, joint: int, dt: float = 1 / 30,
             now: Callable[[], float] = time.time,
             before_frame: Callable[[float], None] | None = None) -> PourController:
    """30 Hz loop: frame -> controller -> stream the tilt to the pour joint. Ends tilted back to base_q.

    joint is 0-based (5 = joint6). before_frame(tilt) lets the fakes step their physics.
    """
    base_q = np.asarray(base_q, dtype=float)
    while controller.active:
        if before_frame:
            before_frame(controller.tilt)
        frame = get_frame()
        cmd = controller.step(frame, now(), dt)
        q = base_q.copy()
        q[joint] += cmd.tilt
        arm.stream_q([q], dt)
        f = controller.last_frame
        telemetry.log("pour.sample", {"t": round(controller.elapsed, 3), "tilt": round(cmd.tilt, 4),
                                      "ml": f and round(f.ml, 3), "flow": f and round(f.flow_ml_s, 3),
                                      "color": f and f.color, "state": cmd.state.value,
                                      "action": cmd.action, "reason": cmd.reason})
    # tilt back at a controlled rate
    n = max(1, math.ceil(controller.tilt / (controller.r.back_rate * dt)))
    ramp = []
    for k in range(n - 1, -1, -1):
        q = base_q.copy()
        q[joint] += controller.tilt * k / n
        ramp.append(q)
    arm.stream_q(ramp, dt)
    telemetry.log("pour.end", {"state": controller.state.value, "reason": controller.reason,
                               "ml": controller.last_frame and controller.last_frame.ml})
    return controller


# ---------------------------------------------------------------- transport

class UdpPourFeed:
    """Receives PourFrame JSON datagrams on localhost. latest() never blocks and returns the newest frame."""

    def __init__(self, port: int = 5555, host: str = "127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self._latest: PourFrame | None = None
        self.bad = 0

    def latest(self) -> PourFrame | None:
        while True:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                return self._latest
            try:
                self._latest = PourFrame.from_json(data)
            except (ValueError, TypeError):
                self.bad += 1

    def close(self) -> None:
        self.sock.close()


def send_frame(sock: socket.socket, frame: PourFrame | dict, port: int = 5555, host: str = "127.0.0.1") -> None:
    """For the camera process: sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)."""
    payload = frame.to_json() if isinstance(frame, PourFrame) else json.dumps(frame)
    sock.sendto(payload.encode(), (host, port))
