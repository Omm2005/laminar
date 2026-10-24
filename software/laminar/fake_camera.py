"""Pretend side camera: sends scripted PourFrame JSON over UDP at 30 Hz, the way the real OpenCV code should.

    python -m laminar.fake_camera                      # normal pour
    python -m laminar.fake_camera --scenario wrong_color
    python -m laminar.fake_camera --scenario dry       # nothing ever comes out
    python -m laminar.fake_camera --scenario surge
    python -m laminar.fake_camera --print              # also print each JSON line
"""
from __future__ import annotations

import argparse
import socket
import time

from .pour import PourFrame, send_frame


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["normal", "wrong_color", "dry", "surge", "blocked"], default="normal")
    ap.add_argument("--color", default="blue")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dt, t, ml, seq = 1 / 30, 0.0, 0.0, 0
    print(f"sending '{a.scenario}' frames to udp://127.0.0.1:{a.port} for {a.seconds:.0f} s")
    while t < a.seconds:
        flow = 0.0
        if a.scenario != "dry" and t > 1.5:          # liquid starts after the arm has tilted a bit
            flow = 1.0 if ml < 1.4 else 0.3
            if a.scenario == "surge" and 2.5 < t < 2.9:
                flow = 3.0
        ml += flow * dt
        color = a.color if ml > 0.05 or flow > 0.1 else "none"
        if a.scenario == "wrong_color" and color != "none":
            color = "red" if a.color != "red" else "blue"
        conf = 0.1 if a.scenario == "blocked" and t > 2.5 else 0.97
        seq += 1
        f = PourFrame(ts=time.time(), seq=seq, ml=round(ml, 3), flow_ml_s=round(flow, 3), pouring=flow > 0.1,
                      color=color, color_conf=0.9 if color != "none" else 0.0, conf=conf, level_px=int(ml / 0.06))
        send_frame(sock, f, port=a.port)
        if a.print:
            print(f.to_json())
        t += dt
        time.sleep(dt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
