"""Publish the real bench to the Laminar website (Convex), as live as the link allows: arm joints and two camera views.

Read-only on the robot side: it never opens a camera or talks to the arm. It reads three files that the
bench software already writes and posts them to Convex:

    robotics/arm_status.json   arm_server.py                      -> POST /bench/telemetry (every new status)
    robotics/wrist_latest.jpg  wrist_stream.py, 10 fps            -> POST /bench/frame?cam=wrist (every new frame)
    robotics/side_latest.jpg   the side camera's owner, 15 fps    -> POST /bench/frame?cam=side  (every new frame)

The side camera is owned by whatever process reads the levels (pour_episode.py's Watch, or sense/live_view.py);
both write their newest full frame to robotics/side_latest.jpg (live_view.LatestJpeg). Do not open the camera a
second time from here.

Why it is fast: each post takes ~0.35 s round trip on a kept-alive HTTPS connection (a new connection per post,
as urllib does, costs ~1 s), so each camera has several workers on their own kept-alive connections, each taking
the NEWEST frame not yet sent (frames are never queued up: a stale frame is skipped, not sent late). Joint samples
are read at 20 Hz and everything gathered so far goes in the next post.

Frames are downscaled to --width px wide JPEG (default 640, quality 70, ~45 kB). A failed post is skipped and
counted; nothing here can slow the arm or the pour.

    BENCH_TOKEN in the environment or the repo's .env (never in git; Convex dashboard -> Settings ->
    Environment Variables)
    python publish.py                           # Ctrl+C to stop

Options: --no-side / --no-wrist / --no-joints, --workers 4, --width 640, --site URL.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import cv2  # optional: only for downscaling frames
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None

SITE = os.environ.get("LAMINAR_SITE", "https://befitting-cobra-22.convex.site")


class Poster:
    """One kept-alive HTTPS connection (reconnects after any error). Not thread-safe: one per worker."""

    def __init__(self, site: str, token: str):
        u = urlparse(site)
        self.host, self.https, self.token, self.conn = u.netloc, u.scheme == "https", token, None

    def post(self, path: str, body: bytes, ctype: str) -> str:
        try:
            if self.conn is None:
                cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                self.conn = cls(self.host, timeout=4)
            self.conn.request("POST", path, body, {"Content-Type": ctype, "Authorization": f"Bearer {self.token}"})
            r = self.conn.getresponse()
            data = r.read()
            return f"ok {r.status}" if r.status < 300 else f"HTTP {r.status} {data[:120].decode(errors='replace')}"
        except (OSError, http.client.HTTPException) as e:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            return f"network: {e!r}"


class Counter:
    def __init__(self):
        self.ok = self.skipped = 0
        self.last_error = ""
        self._lock = threading.Lock()

    def add(self, res: str, n: int = 1):
        with self._lock:
            if res.startswith("ok"):
                self.ok += n
            else:
                self.skipped += n
                self.last_error = res


def joints_loop(status: Path, site: str, token: str, stop: threading.Event, c: Counter, hz=20.0):
    """A reader at `hz` gathers every new status; the sender posts all gathered so far, back to back."""
    buf, lock = [], threading.Lock()

    def read():
        last = None
        while not stop.is_set():
            t0 = time.time()
            try:
                m = status.stat().st_mtime
            except OSError:
                m = None
            if m is not None and m != last:
                try:
                    st = json.loads(status.read_text())
                    state = st.get("state") or {}
                    s = {"t": int(time.time() * 1000), "joints": [float(state[f"J{i}"]) for i in range(1, 7)],
                         "move": str(st.get("doing") or st.get("op") or ("moving" if st.get("busy") else "idle"))[:60]}
                    g = (st.get("gripper") or {}).get("pos")
                    if isinstance(g, (int, float)):
                        s["gripperDeg"] = float(g)
                    last = m
                    with lock:
                        buf.append(s)
                        del buf[:-50]  # if the link stalls, keep only the latest few seconds
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # caught mid-write or not started: next tick
            stop.wait(max(0.0, 1.0 / hz - (time.time() - t0)))

    threading.Thread(target=read, daemon=True).start()
    p = Poster(site, token)
    while not stop.is_set():
        with lock:
            batch = buf[:]
            buf.clear()
        if batch:
            c.add(p.post("/bench/telemetry", json.dumps({"samples": batch}).encode(), "application/json"), len(batch))
        else:
            stop.wait(0.02)


def frame_bytes(path: Path, width: int) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) < 1000 or cv2 is None:
        return raw if 1000 <= len(raw) <= 500_000 else None
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:  # caught mid-replace
        return None
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return enc.tobytes() if ok and len(enc) <= 500_000 else None


def camera_loop(cam: str, path: Path, site: str, token: str, stop: threading.Event, c: Counter,
                workers=4, width=640):
    """A reader picks up every new frame (polled at 100 Hz, only frames written in the last 2 s); `workers`
    senders, each on its own kept-alive connection, take the newest frame nobody has sent yet."""
    slot = {"seq": 0, "data": None, "taken": 0}
    cond = threading.Condition()

    def read():
        last = None
        while not stop.is_set():
            try:
                m = path.stat().st_mtime
            except OSError:
                m = None
            if m is not None and m != last and time.time() - m < 2.0:
                data = frame_bytes(path, width)
                if data:
                    last = m
                    with cond:
                        slot["seq"] += 1
                        slot["data"] = data
                        cond.notify()
            stop.wait(0.01)

    def send():
        p = Poster(site, token)
        while not stop.is_set():
            with cond:
                while slot["taken"] >= slot["seq"] and not stop.is_set():
                    cond.wait(0.2)
                if stop.is_set():
                    return
                slot["taken"] = slot["seq"]
                data = slot["data"]
            c.add(p.post(f"/bench/frame?cam={cam}", data, "image/jpeg"))

    threading.Thread(target=read, daemon=True).start()
    senders = [threading.Thread(target=send, daemon=True) for _ in range(workers)]
    for t in senders:
        t.start()
    for t in senders:
        t.join()


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
    ap.add_argument("--workers", type=int, default=4, help="parallel uploads per camera")
    ap.add_argument("--width", type=int, default=640, help="frames are sent at most this wide")
    ap.add_argument("--no-joints", action="store_true"); ap.add_argument("--no-side", action="store_true"); ap.add_argument("--no-wrist", action="store_true")
    a = ap.parse_args()
    token = env_token(here)
    if not token:
        print("Set BENCH_TOKEN (Convex dashboard -> Settings -> Environment Variables), or put it in the repo's .env.",
              file=sys.stderr)
        return 2
    root = Path(a.robotics)
    stop, counters = threading.Event(), {}
    jobs = []
    if not a.no_joints:
        jobs.append(("joints", joints_loop, root / "arm_status.json", {}))
    for cam in ("wrist", "side"):
        if not getattr(a, f"no_{cam}"):
            jobs.append((cam, lambda *x, _c=cam, **k: camera_loop(_c, *x, **k), root / f"{cam}_latest.jpg",
                         {"workers": a.workers, "width": a.width}))
    for name, fn, path, kw in jobs:
        counters[name] = Counter()
        threading.Thread(target=fn, args=(path, a.site, token, stop, counters[name]), kwargs=kw, daemon=True,
                         name=name).start()
        print(f"{name:6} <- {path}", flush=True)
    print(f"publishing to {a.site}  (Ctrl+C to stop){'' if cv2 else '  [no OpenCV: frames sent as-is]'}", flush=True)
    prev = {n: 0 for n in counters}
    try:
        while True:
            time.sleep(10)
            parts = []
            for n, c in counters.items():
                rate = (c.ok - prev[n]) / 10
                prev[n] = c.ok
                parts.append(f"{n}: {c.ok} sent ({rate:.1f}/s), {c.skipped} skipped"
                             + (f" ({c.last_error})" if c.skipped else ""))
            print(time.strftime("%H:%M:%S") + "  " + "  ".join(parts), flush=True)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
