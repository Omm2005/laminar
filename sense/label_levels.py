"""Step 2 of training the level model: one click per frame on the TOP of the liquid.

Liquid fills from the bottom, so everything below your click counts as liquid. Shows each
recorded frame's column crop, magnified. Labels are saved after every click, so you can quit
and continue later.

    python label_levels.py                    # the target columns from level_calib.json (A, B, ...)
    python label_levels.py --cols B

For recordings without a level calibration use label_scene.py (mark the columns per setup).

Keys:  click = liquid top   SPACE = same level as this column's last frame (cyan line)
       e = empty   f = full to the box top   s = skip (can't tell)   b = back one   q = quit
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
PAD = 12          # px of context shown around the box
SCALE = 4         # magnification


def run_labeler(data: Path, todo: list[tuple[str, str, list]], labels: dict, labels_path: Path,
                make_value=lambda level, roi: level) -> bool:
    """Click-label todo = [(frame, key, roi)]. Saves labels[key] = make_value(level or None, roi) after
    every label. Returns False if the user quit early."""
    click = {"y": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            click["y"] = y

    win = "label: click liquid top | SPACE same as last  e empty  f full  s skip  b back  q quit"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    history: list[str] = []
    last_level: dict[str, float | None] = {}     # per column: the level given on its previous frame
    i = 0
    while i < len(todo):
        fname, key_id, (x, y, w, h) = todo[i]
        img = cv2.imread(str(data / fname))
        y0, y1 = max(0, y - PAD), min(img.shape[0], y + h + PAD)
        x0, x1 = max(0, x - 3 * PAD), min(img.shape[1], x + w + 3 * PAD)
        view = cv2.resize(img[y0:y1, x0:x1], None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
        bx, by = (x - x0) * SCALE, (y - y0) * SCALE
        cv2.rectangle(view, (bx, by), (bx + w * SCALE, by + h * SCALE), (0, 200, 0), 1)
        panel = np.zeros((70, max(view.shape[1], 420), 3), np.uint8)
        cv2.putText(panel, f"{i + 1}/{len(todo)}  column {key_id.rsplit('|', 1)[1]}", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(panel, Path(fname).parent.name + "/" + Path(fname).name, (8, 48),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1, cv2.LINE_AA)
        col_id = key_id.rsplit("|", 1)[1]
        prev = last_level.get(col_id)
        if prev is not None:                         # show last frame's level: SPACE keeps it
            py = int(by + (h - prev) * SCALE)
            cv2.line(view, (bx - 6, py), (bx + w * SCALE + 6, py), (255, 255, 0), 1)
        if view.shape[1] < panel.shape[1]:
            view = cv2.copyMakeBorder(view, 0, 0, 0, panel.shape[1] - view.shape[1], cv2.BORDER_CONSTANT)
        cv2.imshow(win, np.vstack([panel, view]))
        click["y"] = None
        level = None
        while True:
            key = cv2.waitKey(20) & 0xFF
            if click["y"] is not None:
                y_img = (click["y"] - panel.shape[0]) / SCALE + y0          # back to frame pixels
                level = float(np.clip(y + h - y_img, 0, h))                  # px above the box floor
                break
            if key == ord(" ") and prev is not None:
                level = prev
                break
            if key in (ord("e"), ord("f"), ord("s"), ord("b"), ord("q")):
                break
        if key == ord("q") and level is None:
            cv2.destroyAllWindows()
            return False
        if key == ord("b") and level is None:
            if history:
                labels.pop(history.pop(), None)
                i = max(0, i - 1)
                labels_path.write_text(json.dumps(labels, indent=1))
            continue
        lev = level if level is not None else 0.0 if key == ord("e") else float(h) if key == ord("f") else None
        labels[key_id] = make_value(lev, [x, y, w, h])
        last_level[col_id] = lev
        history.append(key_id)
        labels_path.write_text(json.dumps(labels, indent=1))
        i += 1
    cv2.destroyAllWindows()
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--calib", default=str(HERE / "level_calib.json"))
    p.add_argument("--cols", default=None, help="comma list, default: all non-empty calibrated columns")
    a = p.parse_args()

    calib = json.loads(Path(a.calib).read_text())
    rois = {k: v["roi"] for k, v in calib["columns"].items()}
    cols = a.cols.split(",") if a.cols else [k for k in rois if k not in set(calib.get("empty", []))]
    data = Path(a.data)
    labels_path = data / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    frames = sorted(str(p.relative_to(data)) for p in (data / "frames").rglob("*.jpg"))
    todo = [(f, f"{f}|{c}", rois[c]) for f in frames for c in cols if f"{f}|{c}" not in labels]
    print(f"{len(frames)} frames x {len(cols)} columns: {len(todo)} to label, {len(labels)} done")
    if todo:
        run_labeler(data, todo, labels, labels_path)
    usable = sum(v is not None for v in labels.values())
    print(f"{usable} usable labels saved to {labels_path}. Next:  python train_seg.py")


if __name__ == "__main__":
    main()
