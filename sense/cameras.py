"""Camera helpers for Laminar.

- open_camera(): open a USB camera with fixed settings (MJPEG, resolution, exposure locked)
- LatestFrame: background thread that always holds the NEWEST frame, so the pour loop
  never reads a stale, buffered frame (stale frames = extra lag = overshoot).
- list_cameras(): quick scan to find which index is which camera.

Tip (Linux): open cameras by a fixed name, e.g.
  /dev/v4l/by-id/usb-<vendor>_<model>-video-index0
because numeric indexes (0, 1, 2) can swap after a replug or reboot.
"""
from __future__ import annotations

import threading
import time

import cv2


def open_camera(src: int | str, width: int = 640, height: int = 480, fps: int = 30,
                mjpg: bool = True, exposure: float | None = None,
                wb_kelvin: int | None = None) -> cv2.VideoCapture:
    """Open a camera and lock its settings. Prints what the camera actually accepted."""
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera {src!r}")
    if mjpg:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if exposure is not None:
        # V4L2 (Linux): 1 = manual, 3 = auto. Other backends use 0.25 / 0.75.
        # Check the printout below; if it didn't stick, use `v4l2-ctl` on Linux.
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
        cap.set(cv2.CAP_PROP_EXPOSURE, exposure)
    if wb_kelvin is not None:
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        cap.set(cv2.CAP_PROP_WB_TEMPERATURE, wb_kelvin)
    print(f"[camera {src}] {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x"
          f"{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f} @ {cap.get(cv2.CAP_PROP_FPS):.0f} fps, "
          f"auto_exp={cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)}, exp={cap.get(cv2.CAP_PROP_EXPOSURE)}")
    return cap


class LatestFrame:
    """Grab frames in a thread; read() returns the newest frame and its timestamp."""

    def __init__(self, cap: cv2.VideoCapture):
        self.cap = cap
        self._frame = None
        self._ts = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.fps_measured = 0.0

    def start(self) -> "LatestFrame":
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        t0 = time.time()
        while self._frame is None and time.time() - t0 < 3:
            time.sleep(0.01)
        if self._frame is None:
            raise RuntimeError("Camera opened but no frames arrived")
        return self

    def _loop(self):
        n, t_start = 0, time.time()
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.005)
                continue
            with self._lock:
                self._frame, self._ts = frame, time.time()
            n += 1
            if n % 30 == 0:
                self.fps_measured = n / (time.time() - t_start)

    def read(self):
        """Return (frame_copy, timestamp). Never blocks for long."""
        with self._lock:
            return (None if self._frame is None else self._frame.copy()), self._ts

    def average(self, n: int = 10, gap_s: float = 0.04):
        """Average n frames (use for clean reference images)."""
        acc = None
        for _ in range(n):
            f, _ = self.read()
            acc = f.astype("float32") if acc is None else acc + f
            time.sleep(gap_s)
        return (acc / n).astype("uint8")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        self.cap.release()


def list_cameras(max_index: int = 6):
    """Print which camera indexes open, and their default resolution."""
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ok, f = cap.read()
            print(f"index {i}: {'OK ' + str(f.shape) if ok else 'opens, no frame'}")
        cap.release()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="List cameras or preview one")
    p.add_argument("--cam", help="index or device path to preview")
    a = p.parse_args()
    if a.cam is None:
        list_cameras()
    else:
        src = int(a.cam) if a.cam.isdigit() else a.cam
        cam = LatestFrame(open_camera(src)).start()
        while True:
            f, _ = cam.read()
            cv2.putText(f, f"{cam.fps_measured:.1f} fps", (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 0, 255), 2)
            cv2.imshow("preview (q to quit)", f)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        cam.stop()
