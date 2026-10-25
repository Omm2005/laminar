"""Publish the real bench to the Laminar website (Convex): arm joints and two camera views.

Read-only on the robot side: it never opens a camera or talks to the arm. It reads three files that the
bench software already writes and posts them to Convex:

    robotics/arm_status.json   arm_server.py, ~5x a second       -> POST /bench/telemetry (10 Hz, batched)
    robotics/wrist_latest.jpg  wrist_stream.py, 10 fps            -> POST /bench/frame?cam=wrist (~4 fps)
    robotics/side_latest.jpg   the side camera's owner            -> POST /bench/frame?cam=side  (~4 fps)

The side camera is owned by whatever process reads the levels (pour_episode.py's Watch, or sense/live_view.py);
both write their newest full frame to robotics/side_latest.jpg (live_view.LatestJpeg). Do not open the camera a
second time from here.

Frames are downscaled to 640 px wide JPEG (quality 70, ~30-60 kB) when OpenCV is available. A failed post is
skipped and counted; nothing here can slow the arm or the pour.

    BENCH_TOKEN in the environment or the repo's .env (never in git; Convex dashboard -> Settings ->
    Environment Variables)
    python publish.py                           # Ctrl+C to stop

Options: --no-side / --no-wrist / --no-joints, --fps 4, --site URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import cv2  # optional: only for downscaling frames
except ImportError:  # pragma: no cover
    cv2 = None

SITE = os.environ.get("LAMINAR_SITE", "https://befitting-cobra-22.convex.site")


def post(site: str, token: str, path: str, body: bytes, ctype: str) -> str:
    req = urllib.request.Request(f"{site.rstrip('/')}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return f"ok {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.read()[:120].decode(errors='replace')}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"network: {e}"


class Counter:
    def __init__(self): self.ok = self.skipped = 0; self.last_error = ""
    def add(self, res: str, n: int = 1):
        if res.startswith("ok"): self.ok += n
        else: self.skipped += n; self.last_error = res


def joints_loop(status: Path, site: str, token: str, stop: threading.Event, c: Counter, hz=10.0, batch=2):
    buf, last = [], None
    while not stop.is_set():
        t0 = time.time()
        try:
            m = status.stat().st_mtime
        except OSError:
            m = None
        if m is not None and m != last:
            last = m
            try:
                st = json.loads(status.read_text())
                state = st.get("state") or {}
                s = {"t": int(time.time() * 1000), "joints": [float(state[f"J{i}"]) for i in range(1, 7)],
                     "move": str(st.get("doing") or st.get("op") or ("moving" if st.get("busy") else "idle"))[:60]}
                g = (st.get("gripper") or {}).get("pos")
                if isinstance(g, (int, float)):
                    s["gripperDeg"] = float(g)
                buf.append(s)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # caught mid-write or not started: next tick
        if len(buf) >= batch:
            c.add(post(site, token, "/bench/telemetry", json.dumps({"samples": buf}).encode(), "application/json"), len(buf))
            buf = []
        stop.wait(max(0.0, 1.0 / hz - (time.time() - t0)))


def frame_bytes(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < 1000 or cv2 is None:
        return raw if 1000 <= len(raw) <= 500_000 else None
    import numpy as np
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:  # caught mid-replace
        return None
    h, w = img.shape[:2]
    if w > 640:
        img = cv2.resize(img, (640, int(h * 640 / w)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return enc.tobytes() if ok else None


def camera_loop(cam: str, path: Path, site: str, token: str, stop: threading.Event, c: Counter, fps=4.0):
    last = None
    while not stop.is_set():
        t0 = time.time()
        try:
            m = path.stat().st_mtime
        except OSError:
            m = None
        # only send a frame the camera owner wrote in the last 2 s, and only once
        if m is not None and m != last and time.time() - m < 2.0:
            data = frame_bytes(path)
            if data:
                last = m
                c.add(post(site, token, f"/bench/frame?cam={cam}", data, "image/jpeg"))
        stop.wait(max(0.0, 1.0 / fps - (time.time() - t0)))


def env_token(here: Path) -> str | None:
    """BENCH_TOKEN from the environment, else from the repo's .env (gitignored)."""
    if os.environ.get("BENCH_TOKEN"):
        return os.environ["BENCH_TOKEN"]
    for env in (here / ".env", here.parent / ".env"):
        try:
            for line in env.read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip() == "BENCH_TOKEN" and v.strip():
                    return v.strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robotics", default=str(here), help="folder with arm_status.json and the *_latest.jpg files")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--fps", type=float, default=4.0, help="camera frames per second, per camera")
    ap.add_argument("--no-joints", action="store_true"); ap.add_argument("--no-side", action="store_true"); ap.add_argument("--no-wrist", action="store_true")
    a = ap.parse_args()
    token = env_token(here)
    if not token:
        print("Set BENCH_TOKEN (Convex dashboard -> Settings -> Environment Variables), or put it in the repo's .env.",
              file=sys.stderr)
        return 2
    root = Path(a.robotics)
    stop, counters, threads = threading.Event(), {}, []
    jobs = []
    if not a.no_joints: jobs.append(("joints", joints_loop, (root / "arm_status.json",)))
    if not a.no_wrist: jobs.append(("wrist", lambda *x, **k: camera_loop("wrist", *x, **k), (root / "wrist_latest.jpg",)))
    if not a.no_side: jobs.append(("side", lambda *x, **k: camera_loop("side", *x, **k), (root / "side_latest.jpg",)))
    for name, fn, (path,) in jobs:
        counters[name] = Counter()
        kw = {"fps": a.fps} if name != "joints" else {}
        t = threading.Thread(target=fn, args=(path, a.site, token, stop, counters[name]), kwargs=kw, daemon=True, name=name)
        t.start(); threads.append(t)
        print(f"{name:6} <- {path}", flush=True)
    print(f"publishing to {a.site}  (Ctrl+C to stop){'' if cv2 else '  [no OpenCV: frames sent as-is]'}", flush=True)
    try:
        while True:
            time.sleep(10)
            print(time.strftime("%H:%M:%S") + "  " + "  ".join(
                f"{n}: {c.ok} sent, {c.skipped} skipped" + (f" ({c.last_error})" if c.skipped else "") for n, c in counters.items()), flush=True)
    except KeyboardInterrupt:
        stop.set()
        for t in threads: t.join(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
