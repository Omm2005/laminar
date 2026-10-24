"""Grab one frame from the wrist camera.  Usage: python snap.py OUT.jpg [--camera 2]"""
import argparse

import cv2

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--camera", type=int, default=2)
a = ap.parse_args()
cap = cv2.VideoCapture(a.camera, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
for _ in range(10):  # let exposure settle
    ok, frame = cap.read()
cap.release()
if not ok:
    raise SystemExit("no frame")
cv2.imwrite(a.out, frame)
print("saved", a.out)
