"""Teleop episode recorder: camera video + synchronized joint data at a fixed frame rate.

Output folder recordings/<YYYYmmdd_HHMMSS>/ contains:
  video.mp4  - camera frames at exactly --fps (latest camera frame sampled each tick)
  data.csv   - one row per video frame: frame, t (s since start), action.J1..J7 (leader target),
               command.J1..J7 (rate-limited follower command), state.J1..J7 (measured follower position);
               all in degrees relative to the follower zero pose
  meta.json  - fps, camera, resolution, joint names, zero offsets
"""
import csv
import json
import threading
import time
from pathlib import Path

import cv2

from leader_arm import HERE

JOINT_NAMES = {1: "shoulder_pan", 2: "shoulder_lift", 3: "elbow_flex", 4: "wrist_flex",
               5: "wrist_yaw", 6: "wrist_roll", 7: "gripper"}


class Camera:
    """Continuously grabs frames on its own thread so readers always get the latest one without blocking."""

    def __init__(self, index, width, height):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera {index}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError(f"camera {index} returned no frame")
        self.frame = frame
        self.shape = frame.shape
        self.frames_grabbed = 0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            ok, frame = self.cap.read()
            if ok:
                self.frame = frame
                self.frames_grabbed += 1

    def close(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self.cap.release()


class Recorder:
    """Samples camera + joint data at a fixed rate on a background thread.

    get_sample() is called each tick and must return (action, command, state) dicts keyed by motor id.
    """

    def __init__(self, camera, get_sample, fps, meta):
        self.camera = camera
        self.get_sample = get_sample
        self.fps = fps
        self.dir = HERE / "recordings" / time.strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True)
        h, w = camera.shape[:2]
        self.writer = cv2.VideoWriter(str(self.dir / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        self.csv_file = open(self.dir / "data.csv", "w", newline="")
        self.csv = csv.writer(self.csv_file)
        ids = list(JOINT_NAMES)
        self.csv.writerow(["frame", "t"] + [f"{kind}.J{m}" for kind in ("action", "command", "state") for m in ids])
        (self.dir / "meta.json").write_text(json.dumps({
            "fps": fps, "resolution": [w, h], "joint_names": JOINT_NAMES, "units": "degrees from follower zero pose",
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), **meta,
        }, indent=2))
        self.frames = 0
        self.late = 0
        self._running = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._running = True
        self.t0 = time.perf_counter()
        self._thread.start()

    def _loop(self):
        ids = list(JOINT_NAMES)
        period = 1.0 / self.fps
        while self._running:
            target = self.t0 + self.frames * period
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -period:
                self.late += 1  # writing fell behind; keep the timeline by still emitting this frame
            action, command, state = self.get_sample()
            self.writer.write(self.camera.frame)
            self.csv.writerow([self.frames, f"{self.frames * period:.3f}"]
                              + [f"{d.get(m, float('nan')):.3f}" for d in (action, command, state) for m in ids])
            self.frames += 1

    def stop(self):
        self._running = False
        self._thread.join(timeout=2.0)
        self.writer.release()
        self.csv_file.close()
        return self.dir
