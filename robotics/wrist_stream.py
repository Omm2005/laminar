"""Stream the wrist camera all the time: a live window, and the latest frame in wrist_latest.jpg for other scripts.

Only one process can open a camera on Windows, so this one owns it and markers.py reads wrist_latest.jpg while
it runs (and opens the camera itself when it doesn't). Close with q / Esc in the window.

    python wrist_stream.py [--camera 1]
"""
import argparse
import os
import time
from pathlib import Path

import cv2

LATEST = Path(__file__).resolve().parent / "wrist_latest.jpg"
SAVE_HZ = 10.0

ap = argparse.ArgumentParser()
ap.add_argument("--camera", type=int, default=1, help="wrist camera index (indices shift after re-plugs)")
a = ap.parse_args()


def open_cam():
    c = cv2.VideoCapture(a.camera, cv2.CAP_DSHOW)
    c.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    # 10 fps, not 30: a third of the USB traffic. The camera shares a USB hub with the arm's CAN adapter, and the CAN
    # dropouts of 2026-10-24 all came after this stream started; one still frame at a time is all the scripts use.
    c.set(cv2.CAP_PROP_FPS, 10)
    return c


cap = open_cam()
t_save = 0.0
misses = 0
while True:
    ok, frame = cap.read()
    if not ok:
        # a missed frame used to end the stream, and a pour episode then stopped at "camera would not open"
        # (07:40, when the side camera started streaming on the same hub). Wait it out, reopen after a few.
        misses += 1
        if misses % 5 == 0:
            print(f"camera {a.camera}: {misses} frames missed, reopening", flush=True)
            cap.release()
            time.sleep(0.5)
            cap = open_cam()
        else:
            time.sleep(0.1)
        continue
    misses = 0
    if time.time() - t_save > 1 / SAVE_HZ:
        t_save = time.time()
        tmp = LATEST.with_suffix(".tmp.jpg")
        cv2.imwrite(str(tmp), frame)
        try:
            os.replace(tmp, LATEST)
        except OSError:
            pass  # a reader has it open right now; next frame
    cv2.imshow(f"wrist camera {a.camera} (q to quit)", frame)
    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
        break
cap.release()
cv2.destroyAllWindows()
