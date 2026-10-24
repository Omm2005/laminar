"""Make level_calib.json for the side webcam. Run once, and again if anything moves.

Everything is MOUSE CLICKS in the camera window, or ENTER in Terminal. No other keys.

Step 1  In the camera window, click 2 corners per column: TOP-LEFT (just below the rim,
        inside the walls), then BOTTOM-RIGHT (on the column's floor). Do A, B, C, D in order.
        Right-click undoes the last click. After the last column it moves on by itself.
Step 2  Empty the columns, press ENTER in Terminal (captures the empty reference).
Step 3  Add liquid to column A with a syringe (--step mL each time), wait ~2 s,
        press ENTER in Terminal to record. Type  n  then ENTER when finished.
        B, C, D copy A's table (same geometry). Use --each to calibrate every column.

Run:  python calibrate_level.py --cam 0 --step 0.5
      python calibrate_level.py --cam 0 --columns 2     (only A and B)
"""
from __future__ import annotations

import argparse
import queue
import threading
import time

import cv2
import numpy as np

from cameras import LatestFrame, open_camera
from level import Column, measure_height, save_columns

NAMES = ["A", "B", "C", "D"]
WIN = "Laminar calibration"


def median_height(cam, col, ref, n=15):
    hs = []
    for _ in range(n):
        f, _ = cam.read()
        h, conf = measure_height(col.crop(f), col.crop(ref))
        if conf >= 0.5:
            hs.append(h)
        time.sleep(0.04)
    return (float(np.median(hs)), len(hs)) if hs else (float("nan"), 0)


def terminal_reader(q: "queue.Queue[str]"):
    """Read lines typed in Terminal without freezing the camera window."""
    while True:
        try:
            q.put(input().strip().lower())
        except EOFError:
            return


def show(frame, lines, boxes):
    for name, (x, y, w, h) in boxes.items():
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 0), 1)
        cv2.putText(frame, name, (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
    for i, t in enumerate(lines):
        cv2.putText(frame, t, (10, 22 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    cv2.imshow(WIN, frame)
    cv2.waitKey(15)


def click_boxes(cam, names):
    clicks: list[tuple[int, int]] = []

    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 2 * len(names):
            clicks.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and clicks:
            clicks.pop()

    cv2.namedWindow(WIN)
    cv2.setMouseCallback(WIN, on_mouse)
    while len(clicks) < 2 * len(names):
        boxes = {}
        for i in range(len(clicks) // 2):
            (x1, y1), (x2, y2) = clicks[2 * i], clicks[2 * i + 1]
            boxes[names[i]] = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        col = names[len(clicks) // 2]
        which = "TOP-LEFT (below rim)" if len(clicks) % 2 == 0 else "BOTTOM-RIGHT (floor)"
        f, _ = cam.read()
        for c in clicks[len(boxes) * 2:]:
            cv2.circle(f, c, 4, (0, 0, 255), -1)
        show(f, [f"Column {col}: click {which}", "right-click = undo"], boxes)
    out = {}
    for i, n in enumerate(names):
        (x1, y1), (x2, y2) = clicks[2 * i], clicks[2 * i + 1]
        out[n] = Column(n, (min(x1, x2), min(y1, y2), max(4, abs(x2 - x1)), max(4, abs(y2 - y1))))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cam", default="0")
    p.add_argument("--step", type=float, default=0.5, help="mL added each time")
    p.add_argument("--columns", type=int, default=4, help="how many columns (A..D)")
    p.add_argument("--each", action="store_true", help="calibrate every column separately")
    p.add_argument("--out", default="level_calib.json")
    a = p.parse_args()
    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    names = NAMES[:a.columns]

    # Step 1: boxes by mouse
    print("STEP 1: click in the CAMERA WINDOW: top-left then bottom-right for each column.")
    print("        (If you can't see the window, look behind Terminal.)")
    columns = click_boxes(cam, names)
    boxes = {k: c.roi for k, c in columns.items()}
    print("boxes:", boxes)
    cv2.imwrite("level_boxes.png", cam.read()[0])

    q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=terminal_reader, args=(q,), daemon=True).start()

    # Step 2: empty reference
    print("\nSTEP 2: make sure the columns are EMPTY, then press ENTER here in Terminal.")
    while q.empty():
        show(cam.read()[0], ["Columns EMPTY? Press ENTER in Terminal"], boxes)
    q.get()
    ref = cam.average(20)
    cv2.imwrite("level_ref.png", ref)
    print("  reference captured.")

    # Step 3: syringe steps
    targets = names if a.each else names[:1]
    for name in targets:
        col = columns[name]
        curve, ml = [(0.0, 0.0)], 0.0
        print(f"\nSTEP 3, column {name}: add {a.step} mL, wait 2 s, press ENTER. "
              f"Type n + ENTER when finished.")
        while True:
            f, _ = cam.read()
            hpx_live, conf = measure_height(col.crop(f), col.crop(ref))
            x, y, w, h = col.roi
            cv2.line(f, (x, int(y + h - hpx_live)), (x + w, int(y + h - hpx_live)), (255, 0, 0), 2)
            show(f, [f"{name}: {ml:.2f} mL recorded, {len(curve) - 1} points",
                     f"live height {hpx_live:.1f} px (conf {conf:.2f})"], {name: col.roi})
            if q.empty():
                continue
            cmd = q.get()
            if cmd == "n":
                break
            ml += a.step
            hpx, n = median_height(cam, col, ref)
            print(f"  {ml:.2f} mL -> {hpx:.1f} px ({n}/15 good frames)")
            if n and hpx > curve[-1][0]:
                curve.append((hpx, ml))
            else:
                print("  !! height did not increase; point skipped. Check the box and backlight.")
        col.curve = curve
        if len(curve) > 1:
            print(f"  {name}: {len(curve) - 1} points, about "
                  f"{curve[-1][0] / curve[-1][1]:.1f} px per mL")

    if not a.each:
        for name, col in columns.items():
            if name != targets[0]:
                col.curve = list(columns[targets[0]].curve)
    save_columns(a.out, columns, {"step_ml": a.step, "ref_image": "level_ref.png"})
    print(f"\nSaved {a.out}. Check it live with:  python level.py --cam {a.cam}")
    cv2.destroyAllWindows()
    cam.stop()


if __name__ == "__main__":
    main()
