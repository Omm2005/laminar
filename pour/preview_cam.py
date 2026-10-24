"""Live preview of a camera on DirectShow. Default is the wrist camera (index 2 per teleop/README).

Keys:
  q / Esc  quit
  s        save a snapshot to pour/snapshots/<timestamp>.jpg

Usage:
  python pour/preview_cam.py [--index 2] [--width 640] [--height 480] [--fps 30]
"""
import argparse
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=2)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera index {args.index}")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)

    snaps_dir = HERE / "snapshots"
    snaps_dir.mkdir(exist_ok=True)
    print(f"camera {args.index} @ {args.width}x{args.height}  (q/Esc = quit, s = snapshot)")

    t0 = time.time()
    frames = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("frame grab failed")
                break
            frames += 1
            fps = frames / max(1e-6, time.time() - t0)
            cv2.putText(frame, f"{fps:.1f} fps  idx={args.index}", (8, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.imshow(f"camera {args.index}", frame)
            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27):
                break
            if k == ord("s"):
                path = snaps_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.jpg"
                cv2.imwrite(str(path), frame)
                print(f"saved {path}")
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
