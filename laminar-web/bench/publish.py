"""Publish the real bench to the Laminar dashboard (Convex): arm joints and two live camera views.

Read-only on the robot side: it never opens a camera and never talks to the arm. It reads files the bench
software writes and posts them to Convex over persistent HTTPS connections (one per stream, no handshake per
frame):

    robotics/arm_status.json   arm_server.py, ~5x a second          -> POST /bench/telemetry  (10 Hz, batched)
    robotics/wrist_latest.jpg  wrist_stream.py (owns the wrist cam)  -> POST /bench/frame?cam=wrist
    robotics/side_latest.jpg   the side camera's one owner           -> POST /bench/frame?cam=side

A camera can have exactly ONE owner (Windows refuses a second one). The owner writes its newest frame to the
file (.tmp + os.replace); this script and every other reader only read the file.

Each frame is sent the moment its file changes (up to --fps), downscaled to 480 px wide (960 px for a wide strip
like the side camera's tray crop), JPEG quality 60, when OpenCV is installed. On the dashboard a Convex live query pushes it to every open page.

    set BENCH_TOKEN=...          (PowerShell: $env:BENCH_TOKEN="...")   from Convex -> Settings -> Environment Variables
    python publish.py            (Ctrl+C to stop; prints sent/skipped and upload time every 10 s)

Options: --no-side / --no-wrist / --no-joints, --fps 10, --width 480, --site URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import http.client
import urllib.parse
from pathlib import Path

try:
    import cv2  # optional: only for downscaling frames
except ImportError:  # pragma: no cover
    cv2 = None

SITE = os.environ.get("LAMINAR_SITE", "https://befitting-cobra-22.convex.site")


class Poster:
    """One persistent HTTPS connection (reused for every request; reconnects after an error)."""

    def __init__(self, site: str, token: str):
        u = urllib.parse.urlsplit(site)
        self.host, self.https, self.token, self.conn = u.netloc, u.scheme == "https", token, None

    def __call__(self, path: str, body: bytes, ctype: str) -> str:
        for attempt in (1, 2):  # a kept-alive connection the server closed: reconnect once
            try:
                if self.conn is None:
                    cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                    self.conn = cls(self.host, timeout=5)
                self.conn.request("POST", path, body=body, headers={
                    "Content-Type": ctype, "Authorization": f"Bearer {self.token}", "Connection": "keep-alive"})
                r = self.conn.getresponse()
                data = r.read()
                return f"ok {r.status}" if r.status < 300 else f"HTTP {r.status} {data[:120].decode(errors='replace')}"
            except (http.client.HTTPException, OSError) as e:
                try:
                    if self.conn: self.conn.close()
                except OSError:
                    pass
                self.conn = None
                if attempt == 2:
                    return f"network: {e}"
        return "network: unreachable"


class Counter:
    def __init__(self): self.ok = self.skipped = 0; self.last_error = ""; self.ms: list[float] = []
    def add(self, res: str, n: int = 1, ms: float | None = None):
        if res.startswith("ok"):
            self.ok += n
            if ms is not None: self.ms = (self.ms + [ms])[-50:]
        else: self.skipped += n; self.last_error = res
    def report(self) -> str:
        med = sorted(self.ms)[len(self.ms) // 2] if self.ms else None
        return f"{self.ok} sent, {self.skipped} skipped" + (f", upload {med:.0f} ms" if med else "") + (f" ({self.last_error})" if self.skipped else "")


def joints_loop(status: Path, site: str, token: str, stop: threading.Event, c: Counter, hz=10.0, batch=2):
    post, buf, last = Poster(site, token), [], None
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
            t0p = time.time()
            c.add(post("/bench/telemetry", json.dumps({"samples": buf}).encode(), "application/json"), len(buf), (time.time() - t0p) * 1000)
            buf = []
        stop.wait(max(0.0, 1.0 / hz - (time.time() - t0)))


def frame_bytes(path: Path, width: int = 480) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < 1000 or cv2 is None:
        return raw if 1000 <= len(raw) <= 200_000 else None
    import numpy as np
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:  # caught mid-replace
        return None
    h, w = img.shape[:2]
    target = width * 2 if w / h > 2 else width   # a wide strip (the side camera's tray crop) keeps detail
    if w > target:
        img = cv2.resize(img, (target, int(h * target / w)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
    return enc.tobytes() if ok else None


def camera_loop(cam: str, path: Path, site: str, token: str, stop: threading.Event, c: Counter, fps=10.0, width=480, lanes=2):
    """Send each new frame as soon as the owner writes it (checks every 10 ms), at most `fps` a second.
    `lanes` uploads overlap on their own persistent connections, so a slow upload doesn't hold the next frame;
    each frame carries its capture time and the backend drops any that arrive after a newer one."""
    slots = threading.Semaphore(lanes)
    posters = [Poster(site, token) for _ in range(lanes)]
    free = list(range(lanes)); lock = threading.Lock()

    def send(data: bytes, ts_ms: int):
        with lock:
            i = free.pop()
        try:
            t0 = time.time()
            c.add(posters[i](f"/bench/frame?cam={cam}&ts={ts_ms}", data, "image/jpeg"), 1, (time.time() - t0) * 1000)
        finally:
            with lock:
                free.append(i)
            slots.release()

    last, next_ok = None, 0.0
    while not stop.is_set():
        try:
            m = path.stat().st_mtime
        except OSError:
            m = None
        now = time.time()
        if m is not None and m != last and now >= next_ok and now - m < 2.0 and slots.acquire(blocking=False):
            data = frame_bytes(path, width)
            if data:
                last, next_ok = m, now + 1.0 / fps
                threading.Thread(target=send, args=(data, int(m * 1000)), daemon=True).start()
            else:
                slots.release()
        stop.wait(0.01)


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robotics", default=str(here.parent / "robotics"), help="folder with arm_status.json and the *_latest.jpg files")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--fps", type=float, default=10.0, help="max camera frames per second, per camera")
    ap.add_argument("--width", type=int, default=480, help="frame width sent to the dashboard")
    ap.add_argument("--test-file", help=argparse.SUPPRESS)  # latency test: publish one file as cam=test
    ap.add_argument("--no-joints", action="store_true"); ap.add_argument("--no-side", action="store_true"); ap.add_argument("--no-wrist", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("BENCH_TOKEN")
    if not token:
        print("Set BENCH_TOKEN (Convex dashboard -> Settings -> Environment Variables).", file=sys.stderr)
        return 2
    root = Path(a.robotics)
    stop, counters, threads = threading.Event(), {}, []
    jobs = []
    if not a.no_joints: jobs.append(("joints", joints_loop, (root / "arm_status.json",)))
    if not a.no_wrist: jobs.append(("wrist", lambda *x, **k: camera_loop("wrist", *x, **k), (root / "wrist_latest.jpg",)))
    if not a.no_side: jobs.append(("side", lambda *x, **k: camera_loop("side", *x, **k), (root / "side_latest.jpg",)))
    if a.test_file: jobs = [("test", lambda *x, **k: camera_loop("test", *x, **k), (Path(a.test_file),))]
    for name, fn, (path,) in jobs:
        counters[name] = Counter()
        kw = {"fps": a.fps, "width": a.width} if name != "joints" else {}
        t = threading.Thread(target=fn, args=(path, a.site, token, stop, counters[name]), kwargs=kw, daemon=True, name=name)
        t.start(); threads.append(t)
        print(f"{name:6} <- {path}", flush=True)
    print(f"publishing to {a.site}  (Ctrl+C to stop){'' if cv2 else '  [no OpenCV: frames sent as-is]'}", flush=True)
    try:
        while True:
            time.sleep(10)
            print(time.strftime("%H:%M:%S") + "  " + "  ".join(f"{n}: {c.report()}" for n, c in counters.items()), flush=True)
    except KeyboardInterrupt:
        stop.set()
        for t in threads: t.join(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
