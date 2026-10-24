"""Step 1 of training the level model: record side-camera frames.

Fill the column slowly from empty to full while this runs, several times, and CHANGE THINGS on
purpose between and during runs: room lights on/off, phone backlight brighter/dimmer, shadows,
people walking past. The model learns to ignore whatever varies here. ~300-500 frames is plenty.

    python record_frames.py --cam 0                 # 4 frames/s into seg_data/frames/<time>/
    python record_frames.py --cam 0 --fps 2 --session dim_light

Window keys: SPACE pause/resume, q quit. The camera must not move after level calibration.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from cameras import LatestFrame, open_camera

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cam", default="0")
    p.add_argument("--fps", type=float, default=4.0, help="frames saved per second")
    p.add_argument("--session", default=time.strftime("%H%M%S"))
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--calib", default=str(HERE / "level_calib.json"), help="to draw the column boxes")
    a = p.parse_args()

    out = Path(a.data) / "frames" / a.session
    out.mkdir(parents=True, exist_ok=True)
    boxes = {}
    if Path(a.calib).exists():
        boxes = {k: v["roi"] for k, v in json.loads(Path(a.calib).read_text())["columns"].items()}
    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    n, paused, last = len(list(out.glob("*.jpg"))), False, 0.0
    print(f"saving to {out}  (SPACE pause, q quit)")
    while True:
        f, _ = cam.read()
        if f is None:
            time.sleep(0.01)
            continue
        now = time.time()
        if not paused and now - last >= 1.0 / a.fps:
            cv2.imwrite(str(out / f"frame_{n:05d}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 95])
            n, last = n + 1, now
        view = f.copy()
        for name, (x, y, w, h) in boxes.items():
            cv2.rectangle(view, (x, y), (x + w, y + h), (0, 200, 0), 1)
            cv2.putText(view, name, (x, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 200, 0), 1)
        status = "PAUSED" if paused else f"recording {a.fps:g}/s"
        cv2.putText(view, f"{status}  saved {n}   SPACE pause, q quit", (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255) if paused else (0, 200, 0), 2, cv2.LINE_AA)
        cv2.imshow("record frames", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord(" "):
            paused = not paused
    cam.stop()
    cv2.destroyAllWindows()
    print(f"{n} frames in {out}. Next:  python label_levels.py")


if __name__ == "__main__":
    main()
