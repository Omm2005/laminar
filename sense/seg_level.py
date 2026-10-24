"""Tiny segmentation model for the liquid level: marks which pixels of a column crop are liquid.

Unlike measure_height() it needs no empty reference image, so it copes with venue lighting that
changes over the day. Trained by train_seg.py on frames from record_frames.py + label_levels.py.

    from seg_level import SegLevel
    seg = SegLevel("level_seg.onnx")
    height_px, conf = seg.measure(col.crop(frame))      # same units as measure_height()

    tracker = SegLevelTracker(cam, load_columns("level_calib.json"), "level_seg.onnx")
    tracker.start()                                     # no zero() needed
    tracker.read("A")                                   # LevelSample, like LevelTracker

Live check, model (cyan) next to the reference-image method (red):
    python seg_level.py --cam 0
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from level import LevelSample, LevelTracker, liquid_color, measure_height

HERE = Path(__file__).resolve().parent
IN_H, IN_W = 128, 32           # every column crop is resized to this before the model sees it
DEFAULT_MODEL = HERE / "level_seg.onnx"


def prep(crop: np.ndarray) -> np.ndarray:
    """BGR column crop -> model input (1, 3, IN_H, IN_W) float32 in 0..1."""
    x = cv2.resize(crop, (IN_W, IN_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    return x.transpose(2, 0, 1)[None]


def level_from_probs(probs: np.ndarray) -> tuple[float, float]:
    """(liquid rows, confidence) from a (IN_H, IN_W) probability map. Liquid fills from the bottom,
    so the level is the number of liquid rows; confidence drops if rows flip liquid/not-liquid."""
    row = probs.mean(axis=1)
    rows = float(row.sum())
    certainty = float(np.mean(np.abs(row - 0.5) * 2))           # 1 = every row clearly decided
    flips = int(np.count_nonzero(np.diff((row > 0.5).astype(np.int8))))
    conf = certainty * (1.0 if flips <= 1 else max(0.0, 1.0 - 0.25 * (flips - 1)))
    return rows, conf


class SegLevel:
    def __init__(self, model_path: str | Path = DEFAULT_MODEL):
        import onnxruntime as ort
        # two threads that sleep when idle (the default spins on every core): spare cores must stay
        # free for arm_server's motor feedback (all cores busy -> "lost feedback from J1")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads, opts.inter_op_num_threads = 2, 1
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.sess = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name

    def probs(self, crop: np.ndarray) -> np.ndarray:
        logits = self.sess.run(None, {self.inp: prep(crop)})[0][0, 0]
        return 1.0 / (1.0 + np.exp(-logits))

    def measure_many(self, crops: list[np.ndarray]) -> list[tuple[float, float]]:
        """measure() for several crops in one model call."""
        logits = self.sess.run(None, {self.inp: np.concatenate([prep(c) for c in crops])})[0][:, 0]
        out = []
        for crop, lg in zip(crops, logits):
            rows, conf = level_from_probs(1.0 / (1.0 + np.exp(-lg)))
            out.append((rows * crop.shape[0] / IN_H, conf))
        return out

    def measure(self, crop: np.ndarray) -> tuple[float, float]:
        """Liquid height in px of the crop (from its bottom) and confidence 0..1."""
        rows, conf = level_from_probs(self.probs(crop))
        return rows * crop.shape[0] / IN_H, conf


class SegLevelTracker(LevelTracker):
    """LevelTracker that measures with the tiny model instead of the empty reference image."""

    def __init__(self, cam, columns, model_path: str | Path = DEFAULT_MODEL, history_s: float = 2.0):
        super().__init__(cam, columns, history_s)
        self.seg = SegLevel(model_path)

    def zero(self, n: int = 15):
        pass                                   # nothing to capture: the model needs no reference

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._running:
            frame, ts = self.cam.read()
            if frame is None or ts == self._last_ts:
                time.sleep(0.002)
                continue
            self._last_ts = ts
            crops = {name: col.crop(frame) for name, col in self.columns.items()}
            for (name, crop), (px, conf) in zip(crops.items(), self.seg.measure_many(list(crops.values()))):
                col = self.columns[name]
                color, rgb = liquid_color(crop, px)
                s = LevelSample(col.px_to_ml(px), px, conf, ts, color, rgb)
                with self._lock:
                    self._latest[name] = s
                    self._hist[name].append(s)


def _live():
    import argparse
    from cameras import LatestFrame, open_camera
    from level import load_columns, load_reference

    p = argparse.ArgumentParser(description="Live: model (cyan) vs reference-image method (red)")
    p.add_argument("--cam", default="0")
    p.add_argument("--calib", default=str(HERE / "level_calib.json"))
    p.add_argument("--model", default=str(DEFAULT_MODEL))
    a = p.parse_args()
    cols = load_columns(a.calib)
    empty = set(json.loads(Path(a.calib).read_text()).get("empty", []))
    seg = SegLevel(a.model)
    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    time.sleep(0.5)
    f, _ = cam.read()
    ref = load_reference(a.calib, f.shape if f is not None else None)
    while True:
        f, _ = cam.read()
        if f is None:
            continue
        img = f.copy()
        for name, col in cols.items():
            if name in empty:
                continue
            x, y, w, h = col.roi
            crop = col.crop(f)
            t0 = time.perf_counter()
            m_px, m_conf = seg.measure(crop)
            ms = (time.perf_counter() - t0) * 1000
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 200, 0), 1)
            cv2.line(img, (x - 4, int(y + h - m_px)), (x + w + 4, int(y + h - m_px)), (255, 255, 0), 2)
            text = f"{name} model {m_px:.0f}px c{m_conf:.2f} {ms:.1f}ms"
            if ref is not None:
                r_px, r_conf = measure_height(crop, col.crop(ref))
                cv2.line(img, (x, int(y + h - r_px)), (x + w, int(y + h - r_px)), (0, 0, 255), 1)
                text += f" | ref {r_px:.0f}px c{r_conf:.2f}"
            cv2.putText(img, text, (x - 40, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1, cv2.LINE_AA)
        cv2.imshow("model (cyan) vs reference (red)  q = quit", img)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cam.stop()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    _live()
