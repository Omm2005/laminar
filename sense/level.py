"""Side webcam: liquid level in each reservoir column -> mL, 30 times a second.

Setup this assumes:
- Webcam fixed, facing the LONG side of the 12-channel reservoir, so each used column
  (A, B, C, D) shows as its own vertical strip.
- White phone screen (or any even white light) BEHIND the reservoir.
- Dyed liquid. Exposure and white balance locked (see cameras.py).

How it measures: compare each column strip to an EMPTY reference image. Rows where
liquid sits look different (coloured/darker). The topmost liquid row = the level.
Height in pixels -> mL through a calibration table made with a syringe
(calibrate_level.py).

Usage:
    tracker = LevelTracker(cam, load_columns("level_calib.json"))
    tracker.zero()            # with all columns EMPTY (or before a pour, see note)
    tracker.start()
    s = tracker.read("A")     # LevelSample(ml, px, conf, ts, color, rgb)
    f = tracker.flow("A")     # mL/s over the last ~0.3 s
"""
from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class LevelSample:
    ml: float      # volume estimate
    px: float      # liquid height above the column floor, in pixels
    conf: float    # 0..1; treat < 0.5 as "don't trust this frame"
    ts: float      # timestamp of the camera frame (time.time())
    color: str = "none"                       # liquid colour name, see liquid_color()
    rgb: tuple[int, int, int] | None = None   # median liquid colour, 0..255


def measure_height(crop: np.ndarray, ref: np.ndarray, thr_min: float = 25.0,
                   min_dark: float = 30.0) -> tuple[float, float]:
    """Liquid height (px from the bottom of the crop) and confidence.

    crop, ref: same-size BGR images of one column (current frame and empty reference).
    """
    h = crop.shape[0]
    if crop.mean() < min_dark:                       # backlight off or column blocked
        return 0.0, 0.0
    diff = np.linalg.norm(crop.astype(np.float32) - ref.astype(np.float32), axis=2)
    # median across the strip width: ignores wall edges and small glints
    prof = np.median(diff, axis=1)
    prof = np.convolve(np.pad(prof, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    bottom = float(np.median(prof[-3:]))              # bottom 3 rows: works for shallow liquid
    if bottom < thr_min:                              # nothing at the bottom -> empty
        return 0.0, 1.0
    thr = max(thr_min, 0.5 * bottom)                  # halfway between empty and liquid
    liquid = prof > thr
    # walk up from the bottom until liquid stops. The bottom 1-2 rows can miss the threshold
    # (glare on the column floor) while the 3-row median above says liquid: skip them.
    y = h - 1
    while y > h - 3 and not liquid[y]:
        y -= 1
    if not liquid[y]:
        return 0.0, 0.5
    while y > 0 and liquid[y]:
        y -= 1
    if y == 0:                                        # liquid reaches the top of the crop
        return float(h), 0.3
    # sub-pixel: where the profile crosses thr between row centres y and y+1
    a, b = prof[y], prof[y + 1]
    frac = 0.5 if b == a else (thr - a) / (b - a)
    height = h - (y + frac + 0.5)
    # confidence drops if the profile flips liquid/no-liquid many times (noise, bubbles)
    flips = int(np.count_nonzero(np.diff(liquid.astype(np.int8))))
    conf = 1.0 if flips <= 1 else max(0.0, 1.0 - 0.25 * (flips - 1))
    return float(height), conf


# OpenCV hue is 0..179. (upper bound, name), checked in order; anything above 170 is red.
HUE_NAMES = [(8, "red"), (20, "orange"), (33, "yellow"), (85, "green"), (100, "cyan"),
             (130, "blue"), (155, "purple"), (170, "pink"), (180, "red")]


def liquid_color(crop: np.ndarray, height_px: float, min_rows: int = 3,
                 min_sat: int = 50, min_val: int = 50) -> tuple[str, tuple[int, int, int] | None]:
    """Colour of the liquid in one column crop: (name, (r, g, b)).

    Uses the rows below the level, middle 60 % of the width (skips walls and the floor
    rows). Needs the white backlight: without it the colour is the room behind the column.
    name is "none" if there is too little liquid to see, "clear" if it has little colour,
    "dark" if it blocks most of the light.
    """
    h, w = crop.shape[:2]
    top = int(round(h - height_px))
    bottom = h - 2
    if bottom - top < min_rows:
        return "none", None
    x0, x1 = int(0.2 * w), max(int(0.2 * w) + 1, int(0.8 * w))
    region = crop[top:bottom, x0:x1].reshape(-1, 3)
    b, g, r = (int(v) for v in np.median(region, axis=0))
    hue, sat, val = cv2.cvtColor(np.uint8([[[b, g, r]]]), cv2.COLOR_BGR2HSV)[0, 0]
    if val < min_val:
        name = "dark"
    elif sat < min_sat:
        name = "clear"
    else:
        name = next(n for limit, n in HUE_NAMES if hue < limit)
    return name, (r, g, b)


class Column:
    """One reservoir column: where it is in the image, and its px -> mL table."""

    def __init__(self, name: str, roi: tuple[int, int, int, int],
                 curve: list[tuple[float, float]] | None = None):
        self.name = name
        self.roi = tuple(int(v) for v in roi)        # x, y, w, h (bottom of roi = column floor)
        self.curve = sorted(curve or [(0.0, 0.0)])   # [(height_px, ml), ...]

    def crop(self, img: np.ndarray) -> np.ndarray:
        x, y, w, h = self.roi
        return img[y:y + h, x:x + w]

    def px_to_ml(self, px: float) -> float:
        xs, ys = zip(*self.curve)
        if len(xs) < 2:
            return 0.0
        return float(np.interp(px, xs, ys))          # piecewise-linear, clamps at the ends


def load_columns(path: str) -> dict[str, Column]:
    with open(path) as f:
        data = json.load(f)
    return {k: Column(k, v["roi"], v.get("curve")) for k, v in data["columns"].items()}


def load_empty(path: str) -> list[str]:
    """Names of the columns that must stay empty (X1..), from calibrate_level.py."""
    with open(path) as f:
        return list(json.load(f).get("empty", []))


def load_reference(calib_path: str, frame_shape=None) -> np.ndarray | None:
    """The empty reference image saved by calibrate_level.py, or None if missing or the
    wrong size for the camera (then capture a new one with LevelTracker.zero())."""
    with open(calib_path) as f:
        ref_path = json.load(f).get("ref_image")
    ref = cv2.imread(ref_path) if ref_path else None
    if ref is not None and frame_shape is not None and ref.shape != tuple(frame_shape):
        print(f"  {ref_path} is {ref.shape[1]}x{ref.shape[0]}, the camera is not; ignoring it.")
        return None
    return ref


def save_columns(path: str, columns: dict[str, Column], extra: dict | None = None):
    data = {"columns": {k: {"roi": list(c.roi), "curve": [list(p) for p in c.curve]}
                        for k, c in columns.items()}}
    data.update(extra or {})
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


STABLE_WINDOW_S = 0.6   # read_stable: median over this long ...
NOISE_ML = 0.3          # ... below this it reads 0 (the model's noise on an empty column) ...
SMALL_ML = 1.5          # ... and a level under this that flickers more than its own size also reads 0


class LevelTracker:
    """Runs in a thread, measuring every column on every new frame."""

    def __init__(self, cam, columns: dict[str, Column], history_s: float = 2.0):
        self.cam = cam                    # a cameras.LatestFrame
        self.columns = columns
        self.ref: np.ndarray | None = None
        self._hist = {k: collections.deque(maxlen=int(history_s * 40)) for k in columns}
        self._latest = {k: LevelSample(0.0, 0.0, 0.0, 0.0) for k in columns}
        self._lock = threading.Lock()
        self._running = False
        self._last_ts = 0.0

    def zero(self, n: int = 15):
        """Capture the EMPTY reference. Call with no liquid in the measured columns."""
        self.ref = self.cam.average(n)

    def start(self):
        if self.ref is None:
            raise RuntimeError("call zero() first")
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            frame, ts = self.cam.read()
            if frame is None or ts == self._last_ts:
                time.sleep(0.002)
                continue
            self._last_ts = ts
            for name, col in self.columns.items():
                crop = col.crop(frame)
                px, conf = measure_height(crop, col.crop(self.ref))
                color, rgb = liquid_color(crop, px)
                s = LevelSample(col.px_to_ml(px), px, conf, ts, color, rgb)
                with self._lock:
                    self._latest[name] = s
                    self._hist[name].append(s)

    def read(self, name: str) -> LevelSample:
        """Latest sample for one column (median of the last 3 frames). Never blocks."""
        with self._lock:
            last = list(self._hist[name])[-3:]
        if not last:
            return self._latest[name]
        ml = float(np.median([s.ml for s in last]))
        px = float(np.median([s.px for s in last]))
        return LevelSample(ml, px, min(s.conf for s in last), last[-1].ts,
                           last[-1].color, last[-1].rgb)

    def read_stable(self, name: str) -> LevelSample:
        """For people and logs, not for the pour controller: the median of the last STABLE_WINDOW_S, damped to 0
        when it isn't significant: below NOISE_ML, or a small level (< SMALL_ML) flickering more than it reads
        (an empty column at the edge of the view read 0.6-2 mL of glare/people noise). read() stays raw and fast:
        the pour needs the first drops as they land."""
        with self._lock:
            hist = list(self._hist[name])
        if not hist:
            return self.read(name)
        t_end = hist[-1].ts
        win = [s for s in hist if t_end - s.ts <= STABLE_WINDOW_S and s.conf >= 0.3] or hist[-3:]
        mls = np.array([s.ml for s in win])
        ml = float(np.median(mls))
        spread = float(np.percentile(mls, 90) - np.percentile(mls, 10)) if len(mls) > 2 else 0.0
        if ml < NOISE_ML or (ml < SMALL_ML and spread > ml):
            ml = 0.0
        last = hist[-1]
        return LevelSample(ml, float(np.median([s.px for s in win])), float(np.median([s.conf for s in win])),
                           last.ts, last.color, last.rgb)

    def flow(self, name: str, window_s: float = 0.3) -> float:
        """mL per second: straight-line fit over the last window_s seconds."""
        with self._lock:
            hist = [s for s in self._hist[name] if s.conf >= 0.5]
        if len(hist) < 4:
            return 0.0
        t_end = hist[-1].ts
        pts = [(s.ts - t_end, s.ml) for s in hist if t_end - s.ts <= window_s]
        if len(pts) < 4:
            return 0.0
        t, v = np.array(pts).T
        return float(np.polyfit(t, v, 1)[0])


class FakeLevelTracker:
    """Same read()/flow() API, no camera. Volume rises while `pouring_rate` > 0."""

    def __init__(self, names=("A", "B", "C", "D")):
        self.ml = {k: 0.0 for k in names}
        self.pouring_rate = {k: 0.0 for k in names}   # set this from FakeArm
        self._t = time.time()

    def _tick(self):
        now = time.time()
        dt, self._t = now - self._t, now
        for k in self.ml:
            self.ml[k] += self.pouring_rate[k] * dt

    def zero(self, *_):
        self.ml = {k: 0.0 for k in self.ml}

    def start(self):
        pass

    def read(self, name):
        self._tick()
        noise = float(np.random.normal(0, 0.03))
        return LevelSample(self.ml[name] + noise, 0.0, 1.0, time.time())

    def flow(self, name, window_s=0.3):
        return self.pouring_rate[name]

    def read_stable(self, name):
        return self.read(name)


def draw_overlay(img, columns: dict[str, Column], tracker: LevelTracker | None = None,
                 empty=()):
    """Boxes, level line and mL for a live debug view. Columns in `empty` are drawn grey,
    and red with LIQUID if the camera sees liquid in them."""
    for name, col in columns.items():
        x, y, w, h = col.roi
        if name in empty:
            s = tracker.read(name) if tracker else None
            wet = s is not None and s.conf >= 0.5 and s.ml > 0.1
            color = (0, 0, 255) if wet else (150, 150, 150)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 2 if wet else 1)
            cv2.putText(img, f"{name} LIQUID" if wet else name, (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
            continue
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 200, 0), 1)
        if tracker:
            s = tracker.read(name)
            ly = int(y + h - s.px)
            color = (0, 0, 255) if s.conf >= 0.5 else (0, 165, 255)
            cv2.line(img, (x, ly), (x + w, ly), color, 2)
            label = f"{name} {s.ml:.2f}mL" + (f" {s.color}" if s.color != "none" else "")
            cv2.putText(img, label, (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    return img


if __name__ == "__main__":
    import argparse
    from cameras import LatestFrame, open_camera

    p = argparse.ArgumentParser(description="Live level view")
    p.add_argument("--cam", default="0")
    p.add_argument("--calib", default="level_calib.json")
    a = p.parse_args()
    src = int(a.cam) if a.cam.isdigit() else a.cam
    cam = LatestFrame(open_camera(src)).start()
    cols = load_columns(a.calib)
    tr = LevelTracker(cam, cols)
    input("Empty the measured columns, then press Enter to capture the reference...")
    tr.zero()
    tr.start()
    while True:
        f, _ = cam.read()
        cv2.imshow("levels (q quits, z re-zeros)", draw_overlay(f, cols, tr))
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("z"):
            tr.zero()
    cam.stop()
