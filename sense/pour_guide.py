"""Pour guide: watch one column and tell the arm how fast to pour.

The webcam measures how full the column is. The guide turns that into a SPEED (0..1)
for the arm's tilt, slowing down as the liquid gets close to the target:

    fill (of target)   speed
    0 - 50 %           1.00   full speed
    50 - 75 %          0.50   slow
    75 - 90 %          0.25   slower
    90 - 100 %         ramps down smoothly to 0.05, slower and slower
    "about to hit"     STOP   (tilt back now)

"100 %" is the TARGET line, by default 90 % of the column box you drew in calibration
(box top = just below the rim). Set target_frac=1.0 to fill to the box top. Never set
it above 1.0: liquid that is still in the air when the arm tilts back lands after STOP.

STOP happens a little BEFORE the target: when  fill + rate x lag  reaches 100 %,
where lag (default 0.3 s) is the time between "tilt back" and the stream stopping.
Measure lag on the real arm and set it; it is the number that controls overshoot.

Safety, also from the camera:
- HOLD (speed 0) if the camera can't see the column (hand in the way, light changed).
- ABORT if no liquid arrives within 2 s of begin() (stream is missing the column).
  no_rise_s=0 turns this off (the hand demo does, since a person starts late).
- ABORT if a DIFFERENT column starts filling (wrong column).
- ABORT if the level goes past the target + 5 % (overflow).

How Avery's arm code uses it:

    guide = PourGuide(tracker, "A")          # tracker = level.LevelTracker, already started
    guide.begin()
    while True:
        cmd = guide.update()                 # call ~30 times per second
        if cmd.stop or cmd.abort:
            arm.tilt_back()                  # fast tilt back to upright
            break
        arm.set_tilt_rate(MAX_TILT_RATE * cmd.speed)

Try it with no arm (pour by hand and watch the speed bar):
    python pour_guide.py --cam 0 --col A --log pour1.jsonl
"""
from __future__ import annotations

import collections
import json
import time
from dataclasses import asdict, dataclass

import numpy as np

# (fill fraction of target where the step starts, speed from there on)
DEFAULT_STEPS = [(0.0, 1.0), (0.5, 0.5), (0.75, 0.25), (0.9, 0.12)]


@dataclass
class PourCommand:
    speed: float        # 0..1, multiply by the arm's max tilt rate
    stop: bool          # True = target reached (tilt back now)
    abort: bool         # True = something is wrong (tilt back now, report reason)
    phase: str          # "full", "slow", "slower", "final", "hold", "stop", "abort"
    fill: float         # fill as a fraction of the target (1.0 = target)
    ml: float           # volume estimate from calibration
    rate: float         # fill fraction per second (how fast it is rising)
    conf: float
    reason: str = ""
    t: float = 0.0      # seconds since begin()


class PourGuide:
    def __init__(self, tracker, column: str, target_frac: float = 0.9,
                 steps=DEFAULT_STEPS, min_speed: float = 0.05, lag_s: float = 0.3,
                 no_rise_s: float = 2.0, overflow_margin: float = 0.05,
                 wrong_column_ml: float = 0.3, wrong_column_s: float = 0.5,
                 log_path: str | None = None):
        if not 0.1 <= target_frac <= 1.0:
            raise ValueError("target_frac must be between 0.1 and 1.0")
        self.tr, self.col = tracker, column
        self.box_h = float(tracker.columns[column].roi[3])
        self.target_px = target_frac * self.box_h
        self.steps = sorted(steps)
        self.min_speed, self.lag_s = min_speed, lag_s
        self.no_rise_s, self.overflow_margin = no_rise_s, overflow_margin
        self.wrong_column_ml, self.wrong_column_s = wrong_column_ml, wrong_column_s
        self.log_path = log_path
        self._log = None
        self._hist = collections.deque(maxlen=60)
        self._t0 = 0.0
        self._last_rise_t = 0.0
        self._start_fill = 0.0
        self._others0: dict[str, float] = {}
        self._wrong_since: dict[str, float] = {}
        self._low_conf_since: float | None = None
        self.done: PourCommand | None = None

    # ---------- public ----------

    def begin(self):
        """Call right before the arm starts tilting."""
        s = self.tr.read(self.col)
        self._t0 = time.time()
        self._last_rise_t = self._t0
        self._start_fill = s.px / self.target_px
        self._hist.clear()
        self._others0 = {k: self.tr.read(k).ml for k in self.tr.columns if k != self.col}
        self._wrong_since = {}
        self.done = None
        if self.log_path:
            self._log = open(self.log_path, "a")

    def update(self) -> PourCommand:
        """Call often (~30 Hz). Returns what the arm should do right now."""
        if self.done:
            return self.done
        now = time.time()
        t = now - self._t0
        s = self.tr.read(self.col)
        others = {k: self.tr.read(k) for k in self._others0}
        fill = s.px / self.target_px
        self._hist.append((s.ts, fill))
        rate = self._rate()

        def out(speed, phase, stop=False, abort=False, reason=""):
            cmd = PourCommand(round(speed, 3), stop, abort, phase, round(fill, 4),
                              round(s.ml, 3), round(rate, 4), round(s.conf, 2), reason,
                              round(t, 3))
            if self._log:
                rec = {"col": self.col, **asdict(cmd),
                       "others": {k: [round(o.ml, 3), round(o.px, 1), round(o.conf, 2)]
                                  for k, o in others.items()}}
                self._log.write(json.dumps(rec) + "\n")
            if stop or abort:
                self.done = cmd
                if self._log:
                    self._log.close()
                    self._log = None
            return cmd

        # 1. can we trust the camera?
        if s.conf < 0.5:
            self._low_conf_since = self._low_conf_since or now
            if now - self._low_conf_since > 1.0:
                return out(0.0, "abort", abort=True, reason="camera lost the column for 1 s")
            return out(0.0, "hold", reason="low confidence, holding")
        self._low_conf_since = None

        # 2. safety checks
        if fill > 1.0 + self.overflow_margin:
            return out(0.0, "abort", abort=True, reason=f"overflow: {fill:.0%} of target")
        # another column counts as filling only if it reads high, with good confidence,
        # for wrong_column_s in a row (a person moving behind it or an exposure change
        # can flicker one column for a few frames)
        for k, ml0 in self._others0.items():
            o = others[k]
            if o.conf >= 0.5 and o.ml - ml0 > self.wrong_column_ml:
                since = self._wrong_since.setdefault(k, now)
                if now - since >= self.wrong_column_s:
                    return out(0.0, "abort", abort=True,
                               reason=f"column {k} is filling instead (+{o.ml - ml0:.2f} mL)")
            else:
                self._wrong_since.pop(k, None)
        if rate > 0.01:
            self._last_rise_t = now
        if (self.no_rise_s and now - self._last_rise_t > self.no_rise_s
                and fill - self._start_fill < 0.02):
            return out(0.0, "abort", abort=True,
                       reason=f"level not rising after {self.no_rise_s:g} s: stream missing column")

        # 3. stop early enough that liquid still in the air lands on the target
        if fill + max(rate, 0.0) * self.lag_s >= 1.0:
            return out(0.0, "stop", stop=True, reason="target reached")

        # 4. speed schedule
        return out(*self._speed_for(fill))

    # ---------- internals ----------

    def _speed_for(self, fill: float) -> tuple[float, str]:
        names = {0: "full", 1: "slow", 2: "slower"}
        idx = max([i for i, (f0, _) in enumerate(self.steps) if fill >= f0] or [0])
        speed = self.steps[idx][1]
        phase = names.get(idx, "final")
        last_start = self.steps[-1][0]
        if fill >= last_start:                       # final zone: smooth ramp to min_speed
            remaining = (1.0 - fill) / max(1e-6, 1.0 - last_start)
            speed = max(self.min_speed, min(speed, self.steps[-1][1] * remaining
                                            + self.min_speed * (1 - remaining)))
            phase = "final"
        return max(self.min_speed, speed), phase

    def _rate(self) -> float:
        """Fill fraction per second over the last ~0.3 s (straight-line fit)."""
        if len(self._hist) < 4:
            return 0.0
        t_end = self._hist[-1][0]
        pts = [(ts - t_end, f) for ts, f in self._hist if t_end - ts <= 0.3]
        if len(pts) < 4:
            return 0.0
        tt, ff = np.array(pts).T
        if np.ptp(tt) < 1e-6:
            return 0.0
        return float(np.polyfit(tt, ff, 1)[0])


# ---------- live demo: pour by hand, watch the guide ----------

def _demo():
    import argparse
    import queue
    import threading
    import cv2
    from calibrate_level import terminal_reader
    from cameras import LatestFrame, open_camera
    from level import LevelTracker, draw_overlay, load_columns, load_reference

    p = argparse.ArgumentParser(description="Pour by hand and watch the speed the arm would get")
    p.add_argument("--cam", default="0")
    p.add_argument("--calib", default="level_calib.json")
    p.add_argument("--col", default="A")
    p.add_argument("--target", type=float, default=0.9, help="fraction of the column box")
    p.add_argument("--lag", type=float, default=0.3)
    p.add_argument("--no-rise", type=float, default=0.0,
                   help="abort if no liquid arrives within this many s (0 = wait forever)")
    p.add_argument("--log", default=None, help="append every update to this .jsonl file")
    p.add_argument("--rezero", action="store_true",
                   help="capture a new empty reference instead of the one from calibration")
    a = p.parse_args()

    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    cols = load_columns(a.calib)
    if a.col not in cols:
        raise SystemExit(f"column {a.col!r} not in {a.calib} (has {', '.join(cols)})")
    print(f"Loaded {len(cols)} columns from {a.calib}: {', '.join(cols)}")
    tr = LevelTracker(cam, cols)
    win = "pour guide (close with Ctrl+C in Terminal)"

    q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=terminal_reader, args=(q,), daemon=True).start()

    def wait_enter(msg):
        """Keep the camera window live until ENTER is pressed in Terminal."""
        print(msg)
        while q.empty():
            f, _ = cam.read()
            draw_overlay(f, cols, tr if tr.ref is not None else None)
            cv2.putText(f, msg.split(",")[0], (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 0, 255), 2)
            cv2.imshow(win, f)
            cv2.waitKey(15)
        q.get()

    # the empty reference saved by calibrate_level.py (same one the boxes were drawn on)
    ref = None if a.rezero else load_reference(a.calib, cam.read()[0].shape)
    if ref is not None:
        tr.ref = ref
        print("Using the empty reference from calibration. "
              "Use --rezero if the light or camera moved.")
    else:
        wait_enter("Columns EMPTY? Press ENTER in Terminal to capture the reference")
        tr.zero()
    tr.start()
    time.sleep(0.3)
    guide = PourGuide(tr, a.col, target_frac=a.target, lag_s=a.lag,
                       no_rise_s=a.no_rise, log_path=a.log)
    print("(If you can't see the camera window, look behind Terminal.)")
    wait_enter(f"Press ENTER in Terminal, then pour into column {a.col} by hand")
    guide.begin()
    colors = {"full": (0, 200, 0), "slow": (0, 200, 200), "slower": (0, 140, 255),
              "final": (0, 0, 255), "hold": (160, 160, 160), "stop": (255, 255, 255),
              "abort": (255, 0, 255)}
    while True:
        cmd = guide.update()
        f, _ = cam.read()
        draw_overlay(f, cols, tr)
        x, y, w, h = cols[a.col].roi
        ty = int(y + h - guide.target_px)
        cv2.line(f, (x - 10, ty), (x + w + 10, ty), (255, 255, 255), 1)       # target line
        bar_w = int(200 * cmd.speed)
        cv2.rectangle(f, (10, 40), (210, 60), (80, 80, 80), 1)
        cv2.rectangle(f, (10, 40), (10 + bar_w, 60), colors[cmd.phase], -1)
        cv2.putText(f, f"{cmd.phase.upper()}  speed {cmd.speed:.2f}  fill {cmd.fill:.0%}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colors[cmd.phase], 2)
        if cmd.reason and cmd.phase in ("stop", "abort", "hold"):
            cv2.putText(f, cmd.reason, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        colors[cmd.phase], 2)
        cv2.imshow(win, f)
        cv2.waitKey(15)
        if cmd.stop or cmd.abort:
            print(f"{cmd.phase.upper()}: {cmd.reason} at fill {cmd.fill:.0%}, {cmd.ml:.2f} mL")
            cv2.waitKey(3000)                          # keep the final frame on screen
            break
    cv2.destroyAllWindows()
    cam.stop()


if __name__ == "__main__":
    _demo()
