"""Record the wrist camera to video while wrist_stream.py runs: reads its latest frame (wrist_latest.jpg), so it
never opens the camera and never disturbs the stream. python wrist_record.py [--fps 10]
Writes recordings/wrist_video/wrist_<time>.mp4 (local data, not uploaded); stops with Ctrl+C, a STOP_WRIST_REC file
next to this script, or when the stream has been stale for 30 s."""
import argparse
import os
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
LATEST = HERE / "wrist_latest.jpg"
STOP = HERE / "STOP_WRIST_REC"

ap = argparse.ArgumentParser()
ap.add_argument("--fps", type=float, default=10.0)
a = ap.parse_args()
out_dir = HERE / "recordings" / "wrist_video"
out_dir.mkdir(parents=True, exist_ok=True)
STOP.unlink(missing_ok=True)
path = out_dir / f"wrist_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
writer, frames, last_fresh, last = None, 0, time.time(), None
t_next = time.time()
try:
    while not STOP.exists():
        try:
            age = time.time() - LATEST.stat().st_mtime
            frame = cv2.imread(str(LATEST)) if age < 1.0 else None
        except OSError:
            frame = None
        if frame is not None:
            last, last_fresh = frame, time.time()
        elif time.time() - last_fresh > 30:
            print("wrist stream stale for 30 s: stopping")
            break
        if last is not None:
            if writer is None:
                h, w = last.shape[:2]
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), a.fps, (w, h))
                print(f"recording {w}x{h} @ {a.fps:g} fps to {path}", flush=True)
            writer.write(last)
            frames += 1
        t_next += 1.0 / a.fps
        time.sleep(max(0.0, t_next - time.time()))
except KeyboardInterrupt:
    pass
finally:
    if writer:
        writer.release()
    STOP.unlink(missing_ok=True)
    print(f"saved {frames} frames ({frames / a.fps:.0f} s) to {path}")
