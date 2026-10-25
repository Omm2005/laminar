"""Stream the wrist camera all the time: a live window, and the latest frame in wrist_latest.jpg for other scripts.

Only one process can open a camera on Windows, so this one owns it and markers.py reads wrist_latest.jpg while
it runs (and opens the camera itself when it doesn't). Close with q / Esc in the window.

    python wrist_stream.py [--camera 2]
"""
import argparse
import os
import time
from pathlib import Path

import cv2

LATEST = Path(__file__).resolve().parent / "wrist_latest.jpg"
SAVE_HZ = 10.0

ap = argparse.ArgumentParser()
ap.add_argument("--camera", type=int, default=2, help="wrist camera index (indices shift after re-plugs)")
a = ap.parse_args()
cap = cv2.VideoCapture(a.camera, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
t_save = 0.0
while True:
    ok, frame = cap.read()
    if not ok:
        raise SystemExit(f"camera {a.camera} returned no frame")
    if time.time() - t_save > 1 / SAVE_HZ:
        t_save = time.time()
        tmp = LATEST.with_suffix(".tmp.jpg")
        cv2.imwrite(str(tmp), frame)
        try:
            os.replace(tmp, LATEST)
        except PermissionError:
            pass  # a reader has it open right now; next frame
    cv2.imshow(f"wrist camera {a.camera} (q to quit)", frame)
    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
        break
cap.release()
cv2.destroyAllWindows()
