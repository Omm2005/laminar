"""One live camera window: you draw a box around each column, the level model reads them live.

Per column on the image: box, level line, % full, mL, fill rate (mL/s) while it rises.
Columns are named A, B, C, ... left to right. The columns between and beside your boxes are watched too (faint outlines): if one of them
starts filling, WRONG COLUMN says where the liquid is going (e.g. RIGHT of B).
Warnings: OVERFLOW near the rim, and a dimmed reading
when the model is unsure (hand in front, glare). Boxes are saved and reused next time (the camera
does not move after setup); press b in the window to draw them again.

Drawing: drag a box around each column, from the TOP (rim) to the BOTTOM (floor), inside its walls.
Right-click = undo, ENTER = done, q = quit.

    python live_view.py --cam 0                 # draw boxes the first time, reuse them after
    python live_view.py --cam 0 --new-boxes     # draw new boxes
    python live_view.py --replay setup2         # play a recording as if it were the camera
    python live_view.py --video pour.mp4 --new-boxes --save pour_measured.mp4   # a video file

Keys in the live window: q = quit, b = draw the boxes again, c = clear WRONG COLUMN warnings.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from level import Column
from seg_level import DEFAULT_MODEL, SegLevelTracker

HERE = Path(__file__).resolve().parent
TARGETS = {1: "A", 4: "B", 7: "C", 10: "D"}          # channel number -> name, 12-channel reservoir
MAGENTA, GREEN, RED, ORANGE, WHITE, GREY = (255, 0, 255), (0, 200, 0), (0, 0, 255), (0, 165, 255), (240, 240, 240), (150, 150, 150)


class ReplayCam:
    """A recorded session played back like a LatestFrame camera (loops)."""

    def __init__(self, frames: list[Path], fps: float = 4.0):
        self.frames, self.fps = frames, fps
        self.t0 = time.time()
        self._cache: dict[int, np.ndarray] = {}

    def start(self):
        return self

    def read(self):
        i = int((time.time() - self.t0) * self.fps) % len(self.frames)
        if i not in self._cache:
            self._cache[i] = cv2.imread(str(self.frames[i]))
        return self._cache[i], self.t0 + i / self.fps

    def average(self, n: int = 10, gap_s: float = 0.0):
        return self.read()[0].astype(np.float32)

    def stop(self):
        pass


BOXES_FILE = HERE / "live_boxes.json"


class VideoCam:
    """A video file played like a camera, in real time, once (no loop). read() gives (None, None) at the end."""

    def __init__(self, path: str, speed: float = 1.0):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise SystemExit(f"cannot open video {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 10 ** 9
        self.speed = speed
        self.i, self.frame = -1, None
        self.t0 = None
        self.ended = False
        self._lock = threading.Lock()          # the tracker thread and the window both read: one at a time

    def start(self):
        return self

    def first(self):
        ok, f = self.cap.read()
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return f if ok else None

    def read(self):
        with self._lock:
            return self._read()

    def _read(self):
        if self.t0 is None:
            self.t0 = time.time()
        want = int((time.time() - self.t0) * self.fps * self.speed)
        if want >= self.n:                      # played past the last frame: the video is over
            self.ended = True
        while not self.ended and self.i < want:
            ok, f = self.cap.read()
            if not ok:
                self.ended = True
                break
            self.i, self.frame = self.i + 1, f
        if self.ended:
            return None, None
        return self.frame, (self.t0 + self.i / (self.fps * self.speed)) if self.frame is not None else None

    def average(self, n: int = 10, gap_s: float = 0.0):
        return self.read()[0].astype(np.float32)

    def stop(self):
        self.cap.release()


def draw_boxes(frame: np.ndarray, cam=None) -> list[list[int]] | None:
    """Drag a rectangle around each column. Returns boxes [x, y, w, h] left to right, or None (q).
    With a live camera the picture keeps updating while you draw."""
    boxes: list[list[int]] = []
    drag = {"start": None, "now": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            drag["start"], drag["now"] = (x, y), (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and drag["start"]:
            drag["now"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and drag["start"]:
            (x0, y0), (x1, y1) = drag["start"], (x, y)
            if abs(x1 - x0) > 4 and abs(y1 - y0) > 10:
                boxes.append([min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)])
            drag["start"] = None
        elif event == cv2.EVENT_RBUTTONDOWN and boxes:
            boxes.pop()

    win = "draw a box around each column (rim to floor) | right-click undo | ENTER done | q quit"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        if cam is not None:
            new, _ = cam.read()
            if new is not None:
                frame = new
        v = frame.copy()
        for i, (x, y, w, h) in enumerate(sorted(boxes)):
            cv2.rectangle(v, (x, y), (x + w, y + h), MAGENTA, 2)
            cv2.putText(v, chr(ord("A") + i), (x + 2, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, MAGENTA, 2, cv2.LINE_AA)
        if drag["start"]:
            cv2.rectangle(v, drag["start"], drag["now"], (0, 255, 255), 1)
        cv2.rectangle(v, (0, 0), (v.shape[1], 28), (30, 30, 30), -1)
        cv2.putText(v, f"{len(boxes)} box(es). Drag from rim to floor, inside the walls. ENTER when done.",
                    (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1, cv2.LINE_AA)
        cv2.imshow(win, v)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and boxes:
            cv2.destroyWindow(win)
            cv2.waitKey(1)
            return sorted(boxes)
        if key == ord("q") or key == 27:
            cv2.destroyWindow(win)
            return None


def watch_boxes(boxes: list[list[int]], frame_w: int, side: int = 2) -> list[list[int]]:
    """Boxes for the columns you did NOT draw: between your boxes and `side` more beyond each end,
    spaced like the channels (pitch from the spacing of your boxes). Watched for liquid only."""
    boxes = sorted(boxes)
    cx = [x + w / 2 for x, y, w, h in boxes]
    w = int(np.median([b[2] for b in boxes]))
    y = int(np.median([b[1] for b in boxes]))
    h = int(np.median([b[3] for b in boxes]))
    guess = w * 1.12                                         # channel pitch ~ box width + a wall
    pitches, between = [], []
    for c0, c1 in zip(cx, cx[1:]):
        n = max(1, round((c1 - c0) / guess))
        pitches.append((c1 - c0) / n)
        between += [c0 + k * (c1 - c0) / n for k in range(1, n)]
    pitch = float(np.median(pitches)) if pitches else guess
    ends = [cx[0] - k * pitch for k in range(1, side + 1)] + [cx[-1] + k * pitch for k in range(1, side + 1)]
    out = []
    for c in sorted(between + ends):
        x = int(round(c - w / 2))
        if 0 <= x and x + w <= frame_w:
            out.append([x, y, w, h])
    return out


def build(cam, boxes: list[list[int]], seg_model: str, capacity_ml: float, frame_w: int = 640, watch: bool = True):
    """Your boxes are the targets A, B, C, ...; the columns around them are watched (x1, x2, ...)."""
    boxes = sorted(boxes)
    targets = [chr(ord("A") + i) for i in range(len(boxes))]
    extra = watch_boxes(boxes, frame_w) if watch else []
    names = targets + [f"x{i + 1}" for i in range(len(extra))]
    cols = {n: Column(n, tuple(b), [(0.0, 0.0), (float(b[3]), capacity_ml)]) for n, b in zip(names, boxes + extra)}
    tracker = SegLevelTracker(cam, cols, seg_model)
    tracker.start()
    return tracker, names, targets


def _frame_size(cam) -> tuple[int, int] | None:
    f = cam.first() if hasattr(cam, "first") else cam.read()[0]
    t_end = time.time() + 5
    while f is None and time.time() < t_end:
        time.sleep(0.02)
        f = cam.read()[0]
    return (f.shape[1], f.shape[0]) if f is not None else None


def scale_boxes(boxes: list[list[int]], size_from, size_to) -> list[list[int]]:
    """Boxes drawn at one resolution, at another of the same view (ultrawide 1920x1080 -> wide 1280x720)."""
    sx, sy = size_to[0] / size_from[0], size_to[1] / size_from[1]
    return [[round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy)] for b in boxes]


def get_boxes(cam, new: bool, path: Path = BOXES_FILE) -> list[list[int]] | None:
    """The boxes, at this camera's resolution. Saved as {"size": [w, h], "boxes": [...]}, so boxes drawn in
    ultrawide are scaled for a camera opened wide (the pour); an old bare list is taken as drawn at 640x480."""
    if not new and path.exists():
        saved = json.loads(path.read_text())
        boxes, size = (saved["boxes"], tuple(saved["size"])) if isinstance(saved, dict) else (saved, (640, 480))
        now = _frame_size(cam)
        if now and tuple(now) != size:
            if abs(now[0] / now[1] - size[0] / size[1]) > 0.01:
                print(f"WARNING: boxes were drawn at {size[0]}x{size[1]}, a different shape of view than "
                      f"{now[0]}x{now[1]}: redraw them (--new-boxes)")
            boxes = scale_boxes(boxes, size, now)
            print(f"boxes drawn at {size[0]}x{size[1]}, scaled to {now[0]}x{now[1]}")
        print(f"using the boxes saved in {path.name} ({len(boxes)}); add --new-boxes or press b to redraw")
        return boxes
    frame = cam.first() if hasattr(cam, "first") else None     # a video: draw on its first frame
    live = frame is None
    t_end = time.time() + 5
    while frame is None:
        frame, _ = cam.read()
        if time.time() > t_end:
            print("no picture from the camera after 5 s: wrong --cam number, camera used by another app "
                  "(Zoom, Photo Booth, another script), or Terminal has no camera permission "
                  "(System Settings > Privacy & Security > Camera)")
            return None
        time.sleep(0.02)
    print("draw the box(es) in the camera window, then press ENTER (click the window first)")
    boxes = draw_boxes(frame, cam if live else None)
    if boxes:
        path.write_text(json.dumps({"size": [frame.shape[1], frame.shape[0]], "boxes": boxes}))
    return boxes


def side_of(tracker, n: str, targets: list[str]) -> str:
    """'RIGHT of B' / 'LEFT of A': where a watched column is relative to the nearest target."""
    x = tracker.columns[n].roi[0]
    t = min(targets, key=lambda k: abs(tracker.columns[k].roi[0] - x))
    return f"{'RIGHT' if x > tracker.columns[t].roi[0] else 'LEFT'} of {t}"


FLOW_WINDOW_S = 0.3        # fill rate fitted over this long; main() lengthens it for low-fps videos
WRONG_RISE_PX = 3.0        # median of a watched column's recent readings this much above its start ...
WRONG_WINDOW = 10          # ... over its last this-many new frames (at least WRONG_MIN of them usable)
WRONG_MIN = 6              # = WRONG COLUMN (stays until c). A median ignores single bad readings.
WRONG_CONF = 0.3           # readings below this confidence are not used at all
RATE_ZERO = 0.8            # fill rates (mL/s) below this are shown as 0


def steady(state: dict, key: str, value: float, step: float) -> float:
    """The number to SHOW: kept until the reading moves more than one `step` (the last shown digit)
    away, so a reading wobbling by one digit does not flicker."""
    shown = state.setdefault("shown", {})
    if key not in shown or abs(value - shown[key]) > step * 1.5:
        shown[key] = value
    return shown[key]


def shown_rate(state: dict, key: str, flow: float) -> float:
    return steady(state, key, flow if flow >= RATE_ZERO else 0.0, 0.01)     # negative = noise (liquid only goes in)


def draw(frame: np.ndarray, tracker, names: list[str], start_ml: dict, capacity_ml: float, fps: float,
         targets: list[str] | None = None, state: dict | None = None) -> np.ndarray:
    img = frame.copy()
    targets = targets or names
    state = state if state is not None else {}
    streak, latched = state.setdefault("streak", {}), state.setdefault("latched", {})
    seen_ts = state.setdefault("seen_ts", {})
    warnings = [f"WRONG COLUMN: liquid going {side}" for side in latched.values()]
    for n in names:
        col = tracker.columns[n]
        x, y, w, h = col.roi
        s = tracker.read(n)
        full = s.px / h if h else 0.0
        flow = tracker.flow(n, FLOW_WINDOW_S)                   # mL/s
        sure = s.conf >= 0.5
        target = n in targets
        color = MAGENTA if target else GREEN
        if not sure:
            color = GREY
        if sure and full >= 0.98:
            color = RED
            warnings.append(f"OVERFLOW {n}")
        if not target:
            rise_px = (s.ml - start_ml.get(n, s.ml)) / capacity_ml * h
            if s.conf >= WRONG_CONF and seen_ts.get(n) != s.ts:  # each new camera frame once
                seen_ts[n] = s.ts
                streak[n] = (streak.get(n, []) + [rise_px])[-WRONG_WINDOW:]
            recent = streak.get(n, [])
            if len(recent) >= WRONG_MIN and float(np.median(recent)) >= WRONG_RISE_PX and n not in latched:
                latched[n] = side_of(tracker, n, targets)
                warnings.append(f"WRONG COLUMN: liquid going {latched[n]}")
            if n in latched:
                color = RED
        if not target and color != RED:                        # watched column, quiet: just a faint outline
            cv2.rectangle(img, (x, y), (x + w, y + h), (110, 110, 110), 1)
            continue
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2 if target else 1)
        ly = int(round(y + h - s.px))
        cv2.line(img, (x - 3, ly), (x + w + 3, ly), color if color != GREEN else (0, 255, 255), 2)
        cv2.putText(img, n, (x + 2, y - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)
        cv2.putText(img, f"{steady(state, n + '%', full, 0.01):.0%}", (x, y - 17), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    WHITE, 1, cv2.LINE_AA)
        cv2.putText(img, f"{steady(state, n + 'mL', s.ml, 0.01):.2f}mL", (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.33,
                    WHITE, 1, cv2.LINE_AA)
        flow = shown_rate(state, n + "/s", flow)
        if sure and flow > 0:
            cv2.putText(img, f"+{flow:.2f}/s", (x, y + h + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.35, ORANGE, 1, cv2.LINE_AA)
    # banner
    cv2.rectangle(img, (0, 0), (img.shape[1], 26), (30, 30, 30), -1)
    parts = []
    for n in targets:
        s = tracker.read(n)
        h = tracker.columns[n].roi[3]
        parts.append(f"{n} {steady(state, n + '%', s.px / h, 0.01):.0%} {steady(state, n + 'mL', s.ml, 0.01):.2f}mL "
                     f"{shown_rate(state, n + '/s', tracker.flow(n, FLOW_WINDOW_S)):+.2f}/s")
    cv2.putText(img, "   ".join(parts) + f"   [{fps:.0f} fps]", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1, cv2.LINE_AA)
    if warnings:
        cv2.rectangle(img, (0, img.shape[0] - 28), (img.shape[1], img.shape[0]), (0, 0, 160), -1)
        cv2.putText(img, "  ".join(sorted(set(warnings)))[:90], (8, img.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    WHITE, 2, cv2.LINE_AA)
    return img


class Stats:
    """Collects per-column numbers while running; report() prints the summary at the end."""

    def __init__(self, capacity_ml: float):
        self.capacity_ml = capacity_ml
        self.t0 = time.time()
        self.frames = 0
        self.cols: dict[str, dict] = {}
        self.events: list[tuple[float, str]] = []
        self._seen: set[str] = set()

    def update(self, tracker, targets: list[str], state: dict) -> None:
        self.frames += 1
        t = time.time() - self.t0
        for n in targets:
            s = tracker.read(n)
            h = tracker.columns[n].roi[3]
            c = self.cols.setdefault(n, {"n": 0, "unsure": 0, "start": None, "end": None, "max": 0.0,
                                         "max_flow": 0.0, "flow_sum": 0.0, "flow_n": 0, "fill_s": 0.0, "last_t": t})
            c["n"] += 1
            dt, c["last_t"] = t - c["last_t"], t
            if s.conf < 0.5:
                c["unsure"] += 1
                continue
            full = s.px / h if h else 0.0
            if c["start"] is None:
                c["start"] = (full, s.ml)
            c["end"] = (full, s.ml)
            c["max"] = max(c["max"], full)
            flow = tracker.flow(n, FLOW_WINDOW_S)
            if flow > 0.02:
                c["flow_sum"] += flow
                c["flow_n"] += 1
                c["fill_s"] += dt
                c["max_flow"] = max(c["max_flow"], flow)
            if full >= 0.98 and f"OVERFLOW {n}" not in self._seen:
                self._seen.add(f"OVERFLOW {n}")
                self.events.append((t, f"OVERFLOW {n} (reached the rim)"))
        for col, side in state.get("latched", {}).items():
            key = f"WRONG {col}"
            if key not in self._seen:
                self._seen.add(key)
                self.events.append((t, f"WRONG COLUMN: liquid going {side}"))

    def report(self) -> str:
        t = time.time() - self.t0
        out = ["", "=" * 74, f"SUMMARY   {t:.1f} s, {self.frames} frames measured, full column = {self.capacity_ml} mL",
               "=" * 74,
               f"{'col':4} {'start':>13} {'end':>13} {'added':>9} {'max':>5} {'avg rate':>10} {'max rate':>10} "
               f"{'filling':>8} {'unsure':>7}"]
        for n, c in self.cols.items():
            if c["start"] is None:
                out.append(f"{n:4} never measured with confidence ({c['unsure']}/{c['n']} frames unsure)")
                continue
            (f0, m0), (f1, m1) = c["start"], c["end"]
            avg = c["flow_sum"] / c["flow_n"] if c["flow_n"] else 0.0
            out.append(f"{n:4} {f0:5.0%} {m0:5.2f}mL {f1:5.0%} {m1:5.2f}mL {m1 - m0:+7.2f}mL {c['max']:5.0%} "
                       f"{avg:6.2f}mL/s {c['max_flow']:6.2f}mL/s {c['fill_s']:6.1f}s {c['unsure'] / c['n']:6.0%}")
        out.append("-" * 74)
        if self.events:
            out += [f"  {tt:6.1f} s  {e}" for tt, e in self.events]
        else:
            out.append("  no warnings (no overflow, no wrong column)")
        out.append("=" * 74)
        return "\n".join(out)


class FrameSaver:
    """Saves the raw camera frames (up to `fps` a second) to seg_data/frames/<session>/frame_00000.jpg ...
    with times.csv, the format record_frames.py writes, so label_new.py can label it later."""

    def __init__(self, cam, out: Path, fps: float = 10.0):
        self.cam, self.out, self.fps, self.n = cam, out, fps, 0
        out.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        last_ts, next_t = None, 0.0
        with open(self.out / "times.csv", "w") as times:
            times.write("frame,time\n")
            while self._running:
                frame, ts = self.cam.read()
                if frame is None or ts == last_ts or time.time() < next_t:
                    time.sleep(0.005)
                    continue
                last_ts, next_t = ts, time.time() + 1.0 / self.fps
                cv2.imwrite(str(self.out / f"frame_{self.n:05d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                times.write(f"{self.n},{ts:.3f}\n")
                self.n += 1

    def stop(self):
        self._running = False
        self._thread.join(timeout=2)
        print(f"camera recorded: {self.n} frames in {self.out.relative_to(HERE)}")


def cam_width(cam) -> int:
    f = cam.first() if hasattr(cam, "first") else cam.read()[0]
    return f.shape[1] if f is not None else 640


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cam", default="0")
    p.add_argument("--replay", default=None, help="session in seg_data/frames to play instead of the camera")
    p.add_argument("--video", default=None, help="a recorded video file (.mp4/.mov) to play instead of the camera")
    p.add_argument("--speed", type=float, default=1.0, help="--video playback speed")
    p.add_argument("--seg-model", default=str(DEFAULT_MODEL))
    p.add_argument("--capacity-ml", type=float, default=4.5, help="mL in a full column")
    p.add_argument("--new-boxes", action="store_true", help="draw new boxes instead of reusing the saved ones")
    p.add_argument("--boxes", default=str(BOXES_FILE), help="where the boxes are saved")
    p.add_argument("--no-watch", action="store_true", help="do not watch the columns around your boxes")
    p.add_argument("--save", default=None, help="also save the annotated view to this .mp4")
    p.add_argument("--no-record", action="store_true",
                   help="live camera: do not record (by default the raw frames and the annotated view "
                        "review.mp4 go to seg_data/frames/live_<date>_<time>/)")
    p.add_argument("--record-fps", type=float, default=10.0, help="raw camera frames saved per second")
    a = p.parse_args()

    if not Path(a.seg_model).exists():
        raise SystemExit(f"{Path(a.seg_model).name} missing: train it first (train_seg.py)")
    if a.video:
        cam = VideoCam(a.video, a.speed)
    elif a.replay:
        cam = ReplayCam(sorted((HERE / "seg_data" / "frames" / a.replay).glob("*.jpg")))
    else:
        from cameras import LatestFrame, open_camera
        cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
        time.sleep(0.5)
    saver = None
    if not (a.video or a.replay or a.no_record):
        out = HERE / "seg_data" / "frames" / time.strftime("live_%Y%m%d_%H%M%S")
        saver = FrameSaver(cam, out, a.record_fps)
        a.save = a.save or str(out / "review.mp4")
        print(f"recording to {out.relative_to(HERE)} (--no-record to turn off)")
    global FLOW_WINDOW_S
    src_fps = getattr(cam, "fps", 30.0) * getattr(cam, "speed", 1.0)
    FLOW_WINDOW_S = max(0.3, 6.0 / src_fps)                     # at least ~6 frames for the rate fit
    boxes = get_boxes(cam, a.new_boxes, Path(a.boxes))
    if not boxes:
        if saver:
            saver.stop()
        cam.stop()
        return
    tracker, names, targets = build(cam, boxes, a.seg_model, a.capacity_ml, cam_width(cam), not a.no_watch)
    print(f"measuring {' '.join(targets)}; watching {len(names) - len(targets)} columns around them for wrong fills")
    time.sleep(0.4)
    start_ml = {n: tracker.read(n).ml for n in names}
    writer, t_prev, fps, last_ts, state = None, time.time(), 0.0, None, {}
    stats, stats_ts = Stats(a.capacity_ml), None
    out_fps = getattr(cam, "fps", 15.0) * getattr(cam, "speed", 1.0) if a.video else 15.0
    live = not (a.video or a.replay)
    if live:
        out_fps = 10.0                  # live: a steady 10 frames a second of wall time, so it plays at real speed
    next_write = None
    try:
        while True:
            frame, ts = cam.read()
            if frame is None:
                if getattr(cam, "ended", False):
                    print("end of video")
                    break
                time.sleep(0.01)
                continue
            now = time.time()
            fps = 0.9 * fps + 0.1 / max(now - t_prev, 1e-3)
            t_prev = now
            if ts != last_ts or not a.save:
                view = draw(frame, tracker, names, start_ml, a.capacity_ml, fps, targets, state)
            if ts != stats_ts:                                      # stats once per new camera/video frame
                stats.update(tracker, targets, state)
                stats_ts = ts
            if a.save and (live or ts != last_ts):                  # replay/video: one saved frame per frame
                if writer is None:
                    size = (view.shape[1], view.shape[0])
                    writer = cv2.VideoWriter(a.save, cv2.VideoWriter_fourcc(*"avc1"), out_fps, size)   # H.264: QuickTime plays it
                    if not writer.isOpened():
                        writer = cv2.VideoWriter(a.save, cv2.VideoWriter_fourcc(*"mp4v"), out_fps, size)
                if live:
                    next_write = next_write or now
                    while next_write <= now:                        # repeat the view if the loop fell behind
                        writer.write(view)
                        next_write += 1.0 / out_fps
                else:
                    writer.write(view)
                last_ts = ts
            cv2.imshow("live levels  (q quit, b redraw boxes, c clear warnings)", view)
            key = cv2.waitKey(30) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):                                     # clear WRONG COLUMN warnings, new baseline
                state = {}
                start_ml = {n: tracker.read(n).ml for n in names}
            if key == ord("b"):
                tracker.stop()
                boxes = get_boxes(cam, True, Path(a.boxes))
                if not boxes:
                    break
                tracker, names, targets = build(cam, boxes, a.seg_model, a.capacity_ml, cam_width(cam), not a.no_watch)
                time.sleep(0.4)
                start_ml = {n: tracker.read(n).ml for n in names}
                state = {}
    except KeyboardInterrupt:
        print("stopped (Ctrl+C)")
    tracker.stop()
    if saver:
        saver.stop()
    cam.stop()
    if writer:
        writer.release()
        print(f"saved {a.save}")
    cv2.destroyAllWindows()
    print(stats.report())


if __name__ == "__main__":
    main()
