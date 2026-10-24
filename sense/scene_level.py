"""Scene model: finds every column in the whole frame by itself (no level calibration).

The model marks each pixel background / column (empty part) / liquid. Columns are the tall
strips of "column or liquid", split at the walls between them. A column's box runs from its
floor to its rim, so fill = liquid height / box height: no fill marks needed.

Used once at the start of a pour to place the boxes; the live level then comes from the fast
crop model (seg_level.py) on those boxes.

    python scene_level.py --cam 0          # live: detected columns and their fill
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from level import Column

HERE = Path(__file__).resolve().parent
SCENE_H, SCENE_W = 240, 320
DEFAULT_SCENE_MODEL = HERE / "scene_seg.onnx"


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of every run of True in a 1-D mask, end exclusive."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def find_columns(cls: np.ndarray, min_h_frac: float = 0.15) -> list[dict]:
    """Columns from a class map (0 background, 1 column, 2 liquid) at model resolution, left to right."""
    H, W = cls.shape
    col = cls >= 1
    cov = col.sum(0).astype(float)                       # how many rows are column, per x
    if cov.max() < min_h_frac * H:
        return []
    typical = float(np.median(cov[cov > 0.3 * cov.max()]))
    out = []
    for x0, x1 in _runs(cov >= 0.5 * typical):           # walls between columns dip below this
        if x1 - x0 < 2:
            continue
        rows = _runs(col[:, x0:x1].mean(1) > 0.5)
        if not rows:
            continue
        y0, y1 = max(rows, key=lambda r: r[1] - r[0])
        h, w = y1 - y0, x1 - x0
        if h < min_h_frac * H or h < 1.5 * w:
            continue
        liq = (cls[y0:y1, x0:x1] == 2).mean(1)
        level = 0
        for r in range(h - 1, -1, -1):                   # liquid fills from the bottom
            if liq[r] > 0.5:
                level += 1
            else:
                break
        out.append({"roi": (x0, y0, w, h), "fill": level / h})
    return out


class SceneModel:
    def __init__(self, model_path: str | Path = DEFAULT_SCENE_MODEL):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name

    def classes(self, frame: np.ndarray) -> np.ndarray:
        x = cv2.resize(frame, (SCENE_W, SCENE_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        logits = self.sess.run(None, {self.inp: x.transpose(2, 0, 1)[None]})[0][0]
        return logits.argmax(0)

    def detect(self, frame: np.ndarray) -> list[dict]:
        """Columns in frame pixels: [{"roi": (x, y, w, h), "fill": 0..1}], left to right."""
        sx, sy = frame.shape[1] / SCENE_W, frame.shape[0] / SCENE_H
        cols = []
        for c in find_columns(self.classes(frame)):
            x, y, w, h = c["roi"]
            cols.append({"roi": (int(round(x * sx)), int(round(y * sy)), max(1, int(round(w * sx))),
                                 max(1, int(round(h * sy)))), "fill": c["fill"]})
        return cols

    def detect_stable(self, cam, n: int = 10) -> list[dict]:
        """Median boxes over n frames (columns matched by their x centre)."""
        seen: list[list[dict]] = []
        for _ in range(n):
            f, _ = cam.read()
            if f is not None:
                seen.append(self.detect(f))
            time.sleep(0.05)
        if not seen:
            return []
        ref = max(seen, key=len)
        out = []
        for c in ref:
            cx = c["roi"][0] + c["roi"][2] / 2
            same = [d for frame_cols in seen for d in frame_cols
                    if abs(d["roi"][0] + d["roi"][2] / 2 - cx) < c["roi"][2] / 2]
            if len(same) >= n // 2:
                out.append({"roi": tuple(int(np.median([d["roi"][k] for d in same])) for k in range(4)),
                            "fill": float(np.median([d["fill"] for d in same]))})
        return out


def name(i: int) -> str:
    """Columns are named A, B, C, D, ... left to right."""
    return chr(ord("A") + i)


def to_columns(found: list[dict], capacity_ml: float = 4.5) -> dict[str, Column]:
    """Detected boxes -> level.Column named A, B, C, ... (left to right); px -> mL = fill x capacity."""
    return {name(i): Column(name(i), c["roi"], [(0.0, 0.0), (float(c["roi"][3]), capacity_ml)])
            for i, c in enumerate(found)}


def draw(frame: np.ndarray, found: list[dict], target: int | None = None, title: str = "") -> np.ndarray:
    img = frame.copy()
    for i, c in enumerate(found):
        x, y, w, h = c["roi"]
        sel = target == i
        color = (255, 0, 255) if sel else (0, 200, 0)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2 if sel else 1)
        ly = int(y + h - c["fill"] * h)
        cv2.line(img, (x, ly), (x + w, ly), (0, 0, 255), 2)
        cv2.putText(img, name(i), (x + 2, y - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        cv2.putText(img, f"{c['fill']:.0%}", (x, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
    if title:
        cv2.rectangle(img, (0, 0), (img.shape[1], 34), (30, 30, 30), -1)
        cv2.putText(img, title, (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 1, cv2.LINE_AA)
    return img


def confirm_columns(cam, scene: SceneModel, capacity_ml: float = 4.5, target: str | None = None):
    """Detect the columns (named A, B, C, ... left to right), show them, let the operator pick the
    target by its letter (or confirm the preselected one). Returns (columns dict, target name) or
    (None, None) if cancelled. Keys: letter = pick, ENTER = go, 1 = detect again, ESC = cancel."""
    win = "columns: press the LETTER to pour into, ENTER = go, 1 = detect again, ESC = cancel"
    found = scene.detect_stable(cam)
    pick = ord(target.upper()) - ord("A") if target else None
    while True:
        f, _ = cam.read()
        if f is None:
            time.sleep(0.02)
            continue
        if pick is not None and pick >= len(found):
            pick = None
        if not found:
            title = "no columns found: check the camera view. 1 = detect again, ESC = cancel"
        elif pick is None:
            title = f"{len(found)} columns ({name(0)}-{name(len(found) - 1)}). Press the letter to pour into."
        else:
            title = f"pour into {name(pick)}: ENTER = go, another letter = change, 1 = detect again, ESC = cancel"
        cv2.imshow(win, draw(f, found, pick, title))
        key = cv2.waitKey(30) & 0xFF
        if key == 27:
            cv2.destroyWindow(win)
            return None, None
        if key == ord("1"):
            found, pick = scene.detect_stable(cam), None
        elif ord("a") <= key <= ord("z") and key - ord("a") < len(found):
            pick = key - ord("a")
        elif key in (13, 10) and found and pick is not None:
            cv2.destroyWindow(win)
            cv2.waitKey(1)
            return to_columns(found, capacity_ml), name(pick)


def _live():
    import argparse
    from cameras import LatestFrame, open_camera
    p = argparse.ArgumentParser(description="Live: columns found by the scene model")
    p.add_argument("--cam", default="0")
    p.add_argument("--model", default=str(DEFAULT_SCENE_MODEL))
    a = p.parse_args()
    scene = SceneModel(a.model)
    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    while True:
        f, _ = cam.read()
        if f is None:
            continue
        t0 = time.perf_counter()
        found = scene.detect(f)
        ms = (time.perf_counter() - t0) * 1000
        cv2.imshow("scene model (q = quit)", draw(f, found, title=f"{len(found)} columns, {ms:.0f} ms"))
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cam.stop()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    _live()
