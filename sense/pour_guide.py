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
- ABORT if a DIFFERENT column starts filling (wrong column), or if liquid shows up in
  one of the columns that must stay empty (X1..X8 from calibration, `empty=`).
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

Column "auto" (or None): the guide finds the column itself. It watches every column that
isn't an empty one (A..D); the first whose level rises steadily for detect_s becomes the
pour column, and from then on it works as above. Until then the phase is "detect" at full
speed. Two columns rising at once = abort.

    guide = PourGuide(tracker, "auto", empty=load_empty("level_calib.json"))

Try it with no arm (pour by hand and watch the speed bar):
    python pour_guide.py --cam 0 --log pour1.jsonl            # finds the column
    python pour_guide.py --cam 0 --col A --log pour1.jsonl    # column A only
"""
from __future__ import annotations

import collections
import json
import time
from dataclasses import asdict, dataclass

import numpy as np

from level import LevelSample

# (fill fraction of target where the step starts, speed from there on)
DEFAULT_STEPS = [(0.0, 1.0), (0.5, 0.5), (0.75, 0.25), (0.9, 0.12)]


@dataclass
class PourCommand:
    speed: float        # 0..1, multiply by the arm's max tilt rate
    stop: bool          # True = target reached (tilt back now)
    abort: bool         # True = something is wrong (tilt back now, report reason)
    phase: str          # "detect", "full", "slow", "slower", "final", "hold", "stop", "abort"
    fill: float         # fill as a fraction of the target (1.0 = target)
    ml: float           # volume estimate from calibration
    rate: float         # fill fraction per second (how fast it is rising)
    conf: float
    reason: str = ""
    t: float = 0.0      # seconds since begin()
    col: str = ""       # column being poured into ("" while still detecting)


FAST_WRONG_FACTOR = 3.0   # a neighbour rising this many x wrong_column_ml ...
FAST_WRONG_CONF = 0.3     # ... counts even at this (lower) confidence ...
FAST_WRONG_S = 0.1        # ... after only this long: a stream going astray empties a vial in ~1 s


class PourGuide:
    def __init__(self, tracker, column: str, target_frac: float = 0.9,
                 steps=DEFAULT_STEPS, min_speed: float = 0.05, lag_s: float = 0.3,
                 no_rise_s: float = 2.0, overflow_margin: float = 0.05,
                 wrong_column_ml: float = 0.3, wrong_column_s: float = 0.5,
                 empty=(), detect_px: float = 5.0, detect_s: float = 0.3,
                 log_path: str | None = None, watch_near: int | None = None):
        if not 0.1 <= target_frac <= 1.0:
            raise ValueError("target_frac must be between 0.1 and 1.0")
        self.tr = tracker
        self.auto = column in (None, "auto")
        self.col: str | None = None if self.auto else column
        self.target_frac = target_frac
        self.box_h = self.target_px = 0.0
        if self.col:
            self._set_column(self.col)
        self.detect_px, self.detect_s = detect_px, detect_s
        self.steps = sorted(steps)
        self.min_speed, self.lag_s = min_speed, lag_s
        self.no_rise_s, self.overflow_margin = no_rise_s, overflow_margin
        self.wrong_column_ml, self.wrong_column_s = wrong_column_ml, wrong_column_s
        self.empty = set(empty)        # columns that must hold no liquid at all
        self.watch_near = watch_near   # only watch this many columns on each side of the target (None = all):
                                       # a stream can't land 4 channels away, but an edge column can read noise
        self.log_path = log_path
        self._log = None
        self._hist = collections.deque(maxlen=60)
        self._t0 = 0.0
        self._last_rise_t = 0.0
        self._start_fill = 0.0
        self._others0: dict[str, float] = {}
        self._wrong_since: dict[str, float] = {}
        self._low_conf_since: float | None = None
        self._phases: list[dict] = []
        self._n_updates = 0
        self._others_last: dict = {}
        self.last: PourCommand | None = None   # most recent command
        self.done: PourCommand | None = None
        self._px0: dict[str, float] = {}
        self._ml0: dict[str, float] = {}
        self._rise_since: dict[str, float] = {}

    # ---------- public ----------

    def begin(self):
        """Call right before the arm starts tilting."""
        self._t0 = time.time()
        self._last_rise_t = self._t0
        self._hist.clear()
        reads = {k: self.tr.read(k) for k in self.tr.columns}
        self._px0 = {k: r.px for k, r in reads.items()}
        self._ml0 = {k: r.ml for k, r in reads.items()}
        self._rise_since = {}
        if self.auto:
            self.col = None
            # while detecting, only the empty columns are checked for liquid
            self._others0 = {k: 0.0 for k in self.tr.columns if k in self.empty}
        else:
            self._lock(self.col)
        self._wrong_since = {}
        self._phases, self._n_updates, self._others_last = [], 0, {}
        self.last = self.done = None
        if self.log_path:
            self._log = open(self.log_path, "a")

    def update(self) -> PourCommand:
        """Call often (~30 Hz). Returns what the arm should do right now."""
        if self.done:
            return self.done
        now = time.time()
        t = now - self._t0
        detect_abort = ""
        if self.col is None:
            found, detect_abort = self._detect(now)
            if found:
                self._lock(found)
        if self.col:
            s = self.tr.read(self.col)
            fill = s.px / self.target_px
            self._hist.append((s.ts, fill))
        else:
            s, fill = LevelSample(0.0, 0.0, 1.0, now), 0.0
        others = {k: self.tr.read(k) for k in self._others0}
        rate = self._rate()

        def out(speed, phase, stop=False, abort=False, reason=""):
            cmd = PourCommand(round(speed, 3), stop, abort, phase, round(fill, 4),
                              round(s.ml, 3), round(rate, 4), round(s.conf, 2), reason,
                              round(t, 3), self.col or "")
            self._n_updates += 1
            self.last = cmd
            self._others_last = {k: [round(o.ml, 3), round(o.px, 1), round(o.conf, 2)]
                                 for k, o in others.items()}
            if not self._phases or self._phases[-1]["phase"] != phase:
                self._phases.append({"phase": phase, "start_s": cmd.t, "fill_start": cmd.fill})
            self._phases[-1].update(end_s=cmd.t, fill_end=cmd.fill)
            if self._log:
                rec = {"col": self.col, **asdict(cmd), "others": self._others_last}
                self._log.write(json.dumps(rec) + "\n")
            if stop or abort:
                self.done = cmd
                if self._log:
                    self._log.close()
                    self._log = None
            return cmd

        if detect_abort:
            return out(0.0, "abort", abort=True, reason=detect_abort)

        # 0. liquid going into another column. Checked FIRST, before the target's own confidence: when the vial
        # dumps, the vial and the stream hide the target column (its confidence drops, step 1 returns "hold"),
        # and the old order never got here while a neighbour filled 0 -> 4 mL (2026-10-25). A column counts as
        # filling if it reads high with good confidence for wrong_column_s (a person behind it or an exposure
        # change can flicker one column for a few frames), or rises a lot (FAST_WRONG_FACTOR x the threshold)
        # even at lower confidence (a column filling fast reads at ~0.4) for FAST_WRONG_S.
        for k, ml0 in self._others0.items():
            o = others[k]
            rise = o.ml - ml0
            fast = o.conf >= FAST_WRONG_CONF and rise > FAST_WRONG_FACTOR * self.wrong_column_ml
            if (o.conf >= 0.5 and rise > self.wrong_column_ml) or fast:
                since = self._wrong_since.setdefault(k, now)
                if now - since >= (FAST_WRONG_S if fast else self.wrong_column_s):
                    what = ("should be empty but has liquid" if k in self.empty
                            else "is filling instead")
                    return out(0.0, "abort", abort=True,
                               reason=f"column {k} {what} (+{rise:.2f} mL)")
            else:
                self._wrong_since.pop(k, None)

        # 1. can we trust the camera?
        if s.conf < 0.5:
            self._low_conf_since = self._low_conf_since or now
            if now - self._low_conf_since > 1.0:
                return out(0.0, "abort", abort=True, reason="camera lost the column for 1 s")
            return out(0.0, "hold", reason="low confidence, holding")
        self._low_conf_since = None

        # 2. safety checks (the wrong column is step 0)
        if fill > 1.0 + self.overflow_margin:
            return out(0.0, "abort", abort=True, reason=f"overflow: {fill:.0%} of target")
        if rate > 0.01:
            self._last_rise_t = now
        if (self.no_rise_s and now - self._last_rise_t > self.no_rise_s
                and fill - self._start_fill < 0.02):
            return out(0.0, "abort", abort=True,
                       reason=f"level not rising after {self.no_rise_s:g} s: stream missing column")
        if self.col is None:
            return out(1.0, "detect", reason="waiting for liquid in "
                       + ", ".join(k for k in self.tr.columns if k not in self.empty))

        # 3. stop early enough that liquid still in the air lands on the target
        if fill + max(rate, 0.0) * self.lag_s >= 1.0:
            return out(0.0, "stop", stop=True, reason="target reached")

        # 4. speed schedule
        return out(*self._speed_for(fill))

    def summary(self) -> dict:
        """How the pour went: result, final fill, and when each phase ran. Call after
        stop/abort, or any time during the pour: then result is "running" and "now" is
        the latest command."""
        last = self.done
        t = self._phases[-1]["end_s"] if self._phases else 0.0
        frac = self.target_frac
        hold = sum(p["end_s"] - p["start_s"] for p in self._phases if p["phase"] == "hold")
        return {
            "column": self.col,                            # None: never detected one
            "column_detected": self.auto,                  # True: found by the camera
            "result": last.phase if last else "running",   # stop, abort or running
            "reason": last.reason if last else "",
            "now": asdict(self.last) if self.last else None,
            "duration_s": round(t, 3),
            "final_fill_of_target": last.fill if last else None,
            "final_fill_of_box": round(last.fill * frac, 4) if last else None,
            "final_ml": last.ml if last else None,
            "final_rate_per_s": last.rate if last else None,
            "target_frac": frac,
            "lag_s": self.lag_s,
            "updates": self._n_updates,
            "update_hz": round(self._n_updates / t, 1) if t else None,
            "hold_s": round(hold, 2),
            "other_columns_at_end": self._others_last,    # name: [ml, px, conf]
            "phases": self._phases,
        }

    # ---------- internals ----------

    def _set_column(self, name: str):
        self.col = name
        self.box_h = float(self.tr.columns[name].roi[3])
        self.target_px = self.target_frac * self.box_h

    def _lock(self, name: str):
        """Start controlling on this column; every other column is now checked against
        its level at begin() (empty columns against 0)."""
        self._set_column(name)
        self._start_fill = self._px0.get(name, self.tr.read(name).px) / self.target_px
        self._hist.clear()
        near = None
        if self.watch_near is not None:
            order = sorted(self.tr.columns, key=lambda k: self.tr.columns[k].roi[0])   # left to right = channels
            i = order.index(name)
            near = set(order[max(0, i - self.watch_near):i + self.watch_near + 1])
        self._others0 = {k: 0.0 if k in self.empty else self._ml0.get(k, 0.0)
                         for k in self.tr.columns if k != name and (near is None or k in near or k in self.empty)}

    def _detect(self, now: float) -> tuple[str | None, str]:
        """(column, "") once one non-empty column has risen steadily, (None, reason) if
        more than one has, else (None, "")."""
        rising = []
        for k in self.tr.columns:
            if k in self.empty:
                continue
            r = self.tr.read(k)
            if r.conf >= 0.5 and r.px - self._px0.get(k, 0.0) > self.detect_px:
                if now - self._rise_since.setdefault(k, now) >= self.detect_s:
                    rising.append(k)
            else:
                self._rise_since.pop(k, None)
        if len(rising) > 1:
            return None, f"liquid going into several columns at once: {', '.join(rising)}"
        return (rising[0], "") if rising else (None, "")

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
    import os
    import queue
    import threading
    import cv2
    from calibrate_level import terminal_reader
    from cameras import LatestFrame, open_camera
    from level import LevelTracker, draw_overlay, load_columns, load_empty, load_reference
    from liquid_state import write_json

    p = argparse.ArgumentParser(description="Pour by hand and watch the speed the arm would get")
    p.add_argument("--cam", default="0")
    p.add_argument("--calib", default="level_calib.json")
    p.add_argument("--col", default="auto",
                   help="column to pour into, or auto (default): the camera finds it")
    p.add_argument("--target", type=float, default=0.9, help="fraction of the column box")
    p.add_argument("--lag", type=float, default=0.3)
    p.add_argument("--no-rise", type=float, default=0.0,
                   help="abort if no liquid arrives within this many s (0 = wait forever)")
    p.add_argument("--log", default=None, help="append every update to this .jsonl file")
    p.add_argument("--result", default=None,
                   help="write the pour summary here (default: <log>_result.json, "
                        "or pour_result.json without --log)")
    p.add_argument("--rezero", action="store_true",
                   help="capture a new empty reference instead of the one from calibration")
    a = p.parse_args()

    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    cols = load_columns(a.calib)
    if a.col != "auto" and a.col not in cols:
        raise SystemExit(f"column {a.col!r} not in {a.calib} (has {', '.join(cols)})")
    empty = load_empty(a.calib)
    print(f"Loaded {len(cols)} columns from {a.calib}: {', '.join(cols)}")
    if empty:
        print(f"  must stay empty: {', '.join(empty)} (liquid there stops the pour)")
    else:
        print("  no empty columns calibrated: run calibrate_level.py again to watch them")
    tr = LevelTracker(cam, cols)
    win = "pour guide (close with Ctrl+C in Terminal)"

    q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=terminal_reader, args=(q,), daemon=True).start()

    def wait_enter(msg):
        """Keep the camera window live until ENTER is pressed in Terminal."""
        print(msg)
        while q.empty():
            f, _ = cam.read()
            draw_overlay(f, cols, tr if tr.ref is not None else None, empty)
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
                       no_rise_s=a.no_rise, empty=empty, log_path=a.log)
    print("(If you can't see the camera window, look behind Terminal.)")
    where = (", ".join(k for k in cols if k not in empty) if a.col == "auto"
             else f"column {a.col}")
    wait_enter(f"Press ENTER in Terminal, then pour into {where} by hand")
    result = a.result or (os.path.splitext(a.log)[0] + "_result.json" if a.log
                          else "pour_result.json")
    guide.begin()
    print(f"Live summary: {result} (updated 10x a second while pouring)")
    last_write, found = 0.0, None
    colors = {"detect": (255, 200, 0), "full": (0, 200, 0), "slow": (0, 200, 200), "slower": (0, 140, 255),
              "final": (0, 0, 255), "hold": (160, 160, 160), "stop": (255, 255, 255),
              "abort": (255, 0, 255)}
    while True:
        cmd = guide.update()
        if guide.col and guide.col != found:
            found = guide.col
            if a.col == "auto":
                print(f"\n  detected: pouring into column {found}")
        f, _ = cam.read()
        draw_overlay(f, cols, tr, empty)
        if guide.col:
            x, y, w, h = cols[guide.col].roi
            ty = int(y + h - guide.target_px)
            cv2.line(f, (x - 10, ty), (x + w + 10, ty), (255, 255, 255), 1)   # target line
        bar_w = int(200 * cmd.speed)
        cv2.rectangle(f, (10, 40), (210, 60), (80, 80, 80), 1)
        cv2.rectangle(f, (10, 40), (10 + bar_w, 60), colors[cmd.phase], -1)
        cv2.putText(f, f"{cmd.phase.upper()} {guide.col or '?'}  speed {cmd.speed:.2f}  "
                       f"fill {cmd.fill:.0%}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colors[cmd.phase], 2)
        if cmd.reason and cmd.phase in ("stop", "abort", "hold"):
            cv2.putText(f, cmd.reason, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        colors[cmd.phase], 2)
        cv2.imshow(win, f)
        cv2.waitKey(15)
        done = cmd.stop or cmd.abort
        if done or time.time() - last_write >= 0.1:
            last_write = time.time()
            write_json(result, guide.summary())
            print(f"\r  {cmd.t:5.1f} s  {guide.col or '?'}  {cmd.phase:7s} speed {cmd.speed:.2f}  "
                  f"fill {cmd.fill:4.0%}  rate {cmd.rate:+.2f}/s  conf {cmd.conf:.2f}   ",
                  end="", flush=True)
        if done:
            print(f"\n{cmd.phase.upper()}: {cmd.reason} at fill {cmd.fill:.0%}, {cmd.ml:.2f} mL")
            base = result[:-len("_result.json")] if result.endswith("_result.json") \
                else os.path.splitext(result)[0]
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(f, stamp, (10, f.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1)
            images = {"stop": base + "_stop.png", "final": base + "_final.png"}
            cv2.imwrite(images["stop"], f)
            # keep watching 3 s so liquid still in the air lands, then record where it settled
            t_end = time.time() + 3.0
            while time.time() < t_end:
                f, _ = cam.read()
                draw_overlay(f, cols, tr, empty)
                if guide.col:
                    cv2.line(f, (x - 10, ty), (x + w + 10, ty), (255, 255, 255), 1)
                cv2.putText(f, f"settling... {t_end - time.time():.1f} s", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                cv2.imshow(win, f)
                cv2.waitKey(15)
            f, _ = cam.read()
            draw_overlay(f, cols, tr, empty)
            lines = [f"{cmd.phase.upper()}: {cmd.reason}"]
            settled = None
            if guide.col:
                s = tr.read(guide.col)
                settled = {"fill_of_target": round(s.px / guide.target_px, 4),
                           "fill_of_box": round(s.px / guide.box_h, 4),
                           "ml": round(s.ml, 3), "conf": round(s.conf, 2), "color": s.color,
                           "overshoot_of_target": round(s.px / guide.target_px - 1.0, 4)}
                cv2.line(f, (x - 10, ty), (x + w + 10, ty), (255, 255, 255), 1)
                lines += [f"column {guide.col}: stopped at {cmd.fill:.0%}, settled at "
                          f"{settled['fill_of_target']:.0%} of target",
                          f"{settled['ml']:.2f} mL  colour {s.color}  "
                          f"{guide.summary()['duration_s']:.1f} s pour"]
            else:
                lines.append("no column detected")
            for i, line in enumerate(lines):
                cv2.putText(f, line, (10, 30 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            colors[cmd.phase], 2)
            cv2.putText(f, stamp, (10, f.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1)
            cv2.imwrite(images["final"], f)
            cv2.imshow(win, f)
            cv2.waitKey(2000)                          # show the final picture
            summary = guide.summary()
            summary["settled_after_3s"] = settled
            summary["images"] = images
            write_json(result, summary)
            if settled:
                print(f"Column {guide.col} settled at {settled['fill_of_target']:.0%} of target "
                      f"({settled['overshoot_of_target']:+.0%}), {settled['ml']:.2f} mL")
            print(f"Pour summary saved to {result}")
            print(f"Screenshots: {images['stop']} (at stop), {images['final']} (settled)")
            break
    cv2.destroyAllWindows()
    cam.stop()


if __name__ == "__main__":
    _demo()
