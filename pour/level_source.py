"""Level reading contract for pour/.

Backends:
  auto     try opencv, fall back to manual if the camera/calib isn't ready.  (DEFAULT)
  opencv   wraps sense/level.LevelTracker (Amogh's side-cam model, no edits to his files).
  manual   prompts the user via prompt(); read() returns the cached last-typed value.

read() must be cheap (adaptive_pour polls at 5 Hz); the opencv tracker runs its own
background thread so read() is non-blocking.

Channel-to-column mapping is a straight 1->A, 2->B, 3->C, 4->D (what Amogh's
calibrate_level.py labels).  Pass column='X' to override; channels beyond D need
his calibration to add more.

Standalone test:
  python pour/level_source.py --channel 1 [--backend auto|opencv|manual] [--column A]
Prints 10 readings at 5 Hz.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMINAR = HERE.parent
SENSE_DIR = LAMINAR / "sense"
DEFAULT_CALIB = SENSE_DIR / "level_calib.json"
if str(LAMINAR) not in sys.path:
    sys.path.insert(0, str(LAMINAR))

# Default channel-number -> column-letter mapping (edit via `column=` arg if different).
CHANNEL_TO_COLUMN = {1: "A", 2: "B", 3: "C", 4: "D"}


def default_backend() -> str:
    return os.environ.get("POUR_LEVEL", "auto")


def _default_cam_index() -> int:
    return int(os.environ.get("POUR_CAM", "0"))


class LevelSource:
    """Volume source for a reservoir channel.

    `backend='auto'` prefers opencv but falls back to `manual` silently if the camera or
    calib file isn't ready.  Use `backend='opencv'` to raise on failure instead.
    """

    def __init__(self, channel: int, backend: str = "auto", *,
                 column: str | None = None,
                 calib_path: str | os.PathLike | None = None,
                 cam: int | str | None = None):
        self.channel = int(channel)
        self.column = column or CHANNEL_TO_COLUMN.get(self.channel)
        if backend == "opencv" and self.column is None:
            raise ValueError(f"channel {self.channel} has no default column mapping; pass column=...")
        self.backend = "manual"           # updated if opencv init succeeds
        self._last: float = 0.0
        self._tracker = None
        self._cam_wrapper = None

        if backend in ("auto", "opencv"):
            try:
                self._init_opencv(calib_path or DEFAULT_CALIB,
                                  cam if cam is not None else _default_cam_index())
                self.backend = "opencv"
            except Exception as e:
                if backend == "opencv":
                    raise
                print(f"[level_source] opencv init failed ({e!r}); falling back to manual")
                self.backend = "manual"
        elif backend == "manual":
            self.backend = "manual"
        else:
            raise ValueError(f"unknown backend {backend!r}; use auto, opencv, or manual")

    def _init_opencv(self, calib_path, cam):
        """Open the side webcam, load Amogh's columns, zero if no reference is cached, start the thread."""
        if not Path(calib_path).exists():
            raise FileNotFoundError(f"level_calib.json not found at {calib_path}; "
                                    f"run `python sense/calibrate_level.py --cam {cam}` first")
        if self.column is None:
            raise ValueError(f"channel {self.channel} needs an explicit column mapping")
        # import sense.* only when actually needed; keeps manual backend lightweight
        from sense.cameras import open_camera, LatestFrame
        from sense.level import LevelTracker, load_columns, load_reference

        columns = load_columns(str(calib_path))
        if self.column not in columns:
            raise KeyError(f"column {self.column!r} not in {calib_path}; defined: {sorted(columns)}")

        cap = open_camera(cam)
        self._cam_wrapper = LatestFrame(cap).start()

        tracker = LevelTracker(self._cam_wrapper, columns)
        # try the cached empty-reference image first; if missing or wrong size, zero live
        frame, _ = self._cam_wrapper.read()
        tracker.ref = load_reference(str(calib_path), frame.shape if frame is not None else None)
        if tracker.ref is None:
            print("[level_source] no cached empty reference; capturing one now "
                  "(columns must be EMPTY)")
            tracker.zero()
        tracker.start()
        self._tracker = tracker
        # a few samples for the tracker's history to warm up
        time.sleep(0.2)

    def read(self) -> float:
        """Latest mL for self.channel. Cheap, non-blocking."""
        if self.backend == "opencv":
            s = self._tracker.read(self.column)
            self._last = float(s.ml)
            return self._last
        return self._last

    def prompt(self, msg: str = "current mL") -> float:
        """Force a fresh reading. For manual, prompts the user; for opencv, same as read()."""
        if self.backend == "manual":
            while True:
                raw = input(f"[level_source] channel {self.channel}: {msg} > ").strip()
                try:
                    self._last = float(raw)
                    return self._last
                except ValueError:
                    print(f"  not a number: {raw!r}")
        return self.read()

    def close(self):
        if self._tracker is not None:
            try:
                self._tracker.stop()
            except Exception:
                pass
        if self._cam_wrapper is not None:
            try:
                self._cam_wrapper._running = False
                self._cam_wrapper.cap.release()
            except Exception:
                pass


def _standalone_test():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--backend", default=default_backend(), choices=["auto", "opencv", "manual"])
    ap.add_argument("--column", default=None, help="override column (A..D or whatever is in level_calib.json)")
    ap.add_argument("--cam", default=None, help="override camera index (int) or device name")
    ap.add_argument("--n", type=int, default=10, help="number of samples to print")
    ap.add_argument("--hz", type=float, default=5.0)
    args = ap.parse_args()

    kw = {"channel": args.channel, "backend": args.backend, "column": args.column}
    if args.cam is not None:
        kw["cam"] = int(args.cam) if args.cam.isdigit() else args.cam
    lv = LevelSource(**kw)
    print(f"backend active: {lv.backend}   channel {lv.channel} -> column {lv.column!r}")
    if lv.backend == "manual":
        lv.prompt("seed value for the test")
    period = 1.0 / args.hz
    for i in range(args.n):
        t0 = time.time()
        v = lv.read()
        print(f"[{i + 1:2d}/{args.n}]  {v:.3f} mL")
        dt = period - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)
    lv.close()


if __name__ == "__main__":
    _standalone_test()
