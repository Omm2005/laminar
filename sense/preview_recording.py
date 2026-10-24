"""Turn a recorded session into a video (frame numbers burned in) and open it.

    python preview_recording.py setup1          # -> seg_data/setup1_preview.mp4, opens it
    python preview_recording.py setup2 --fps 4  # real speed (recorded at 4 frames/s)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session")
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--fps", type=float, default=8.0, help="playback speed (8 = 2x real time)")
    a = p.parse_args()
    frames = sorted((Path(a.data) / "frames" / a.session).glob("*.jpg"))
    if not frames:
        sys.exit(f"no frames in {Path(a.data) / 'frames' / a.session}")
    out = Path(a.data) / f"{a.session}_preview.mp4"
    h, w = cv2.imread(str(frames[0])).shape[:2]
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"avc1"), a.fps, (w, h))
    if not vw.isOpened():
        vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), a.fps, (w, h))
    for i, f in enumerate(frames):
        img = cv2.imread(str(f))
        cv2.rectangle(img, (0, 0), (210, 30), (30, 30, 30), -1)
        cv2.putText(img, f"frame {i + 1}/{len(frames)}", (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1, cv2.LINE_AA)
        vw.write(img)
    vw.release()
    print(f"{len(frames)} frames -> {out}")
    if sys.platform == "darwin":
        subprocess.run(["open", str(out)])


if __name__ == "__main__":
    main()
