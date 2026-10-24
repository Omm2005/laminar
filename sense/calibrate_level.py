"""Make level_calib.json for the side webcam. Run once, and again if anything moves.

Everything is MOUSE CLICKS in the camera window, or ENTER in Terminal. No other keys.

Step 1  In the camera window, click 2 corners per column: TOP-LEFT (just below the rim,
        inside the walls), then BOTTOM-RIGHT (on the column's floor). Do A, B, C, D in order,
        then the 8 EMPTY columns in any order (named X1..X8). The empty columns are watched
        during every pour: liquid in one of them stops the pour.
        Right-click undoes the last click. After the last column it moves on by itself.
Step 2  Empty the columns, press ENTER in Terminal (captures the empty reference).
Step 3  Add liquid to column A (or --syringe B) with a syringe (--step mL each time), wait ~2 s,
        press ENTER in Terminal to record. Type  n  then ENTER when finished.
        The other columns copy its table (same geometry). Use --each to calibrate every column.
        --percent instead: fill the column by eye to 25, 50, 75, 100 % of full (--marks), ENTER at
        each. Saves fill_curve (px -> fraction of full); mL = fraction x --capacity-ml.

Run:  python calibrate_level.py --cam 0 --step 0.5
      python calibrate_level.py --cam 0 --percent --syringe B --capacity-ml 4.5
      python calibrate_level.py --cam 0 --columns 2     (only A and B)
      python calibrate_level.py --cam 0 --empty 0       (don't watch any empty columns)
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


def steady_height(cam, col, ref, steady_s=1.0, spread_px=3.0, timeout_s=6.0):
    """Wait until the level holds still: steady_s of trusted frames all within spread_px.
    A hand or syringe moving near the column never holds still, so it is never recorded.
    Returns (height_px, "") or (nan, why it never settled)."""
    win: list[float] = []
    t_end = time.time() + timeout_s
    n = int(steady_s / 0.04)
    while time.time() < t_end:
        f, _ = cam.read()
        h, conf = measure_height(col.crop(f), col.crop(ref))
        win = (win + [h])[-n:] if conf >= 0.5 else []   # an untrusted frame restarts the wait
        if len(win) == n and max(win) - min(win) <= spread_px:
            return float(np.median(win)), ""
        time.sleep(0.04)
    return float("nan"), "level never held still (hand or syringe in view? backlight flicker?)"


def percent_steps(cam, q, col, ref, name, marks):
    """Fill the column by eye to each mark (% of full) and record the steady height at each.
    Returns [(height_px, fraction_full), ...] starting at (0, 0)."""
    curve = [(0.0, 0.0)]
    i = 0
    print(f"\nSTEP 3, column {name}: fill it by eye to {', '.join(f'{m:g}%' for m in marks)} of FULL.\n"
          f"        At each mark: move your hand AWAY, press ENTER (it waits for the level to hold still).\n"
          f"        r + ENTER = measure this mark again.  n + ENTER = stop early.")
    print(f"  -> fill to {marks[0]:g}%")
    while i < len(marks):
        f, _ = cam.read()
        hpx_live, conf = measure_height(col.crop(f), col.crop(ref))
        x, y, w, h = col.roi
        cv2.line(f, (x, int(y + h - hpx_live)), (x + w, int(y + h - hpx_live)), (255, 0, 0), 2)
        show(f, [f"{name}: fill to {marks[i]:g}% of full, then ENTER",
                 f"live height {hpx_live:.1f} px (conf {conf:.2f})"], {name: col.roi})
        if q.empty():
            continue
        if q.get() == "n":
            break
        print(f"  {marks[i]:g}%: waiting for the level to hold still ...")
        hpx, why = steady_height(cam, col, ref)
        if why:
            print(f"  !! {why}. NOT recorded: move away, then ENTER again.")
            continue
        prev = curve[-1][0]
        steps = [b[0] - a_[0] for a_, b in zip(curve, curve[1:])]
        typical = float(np.median(steps)) if steps else None
        if hpx <= prev:
            print(f"  !! {hpx:.1f} px is not above the last mark ({prev:.1f} px). NOT recorded: "
                  f"check the box and backlight, then ENTER again.")
        elif typical and hpx - prev > 2.5 * typical:
            print(f"  !! jumped {hpx - prev:.1f} px, usual step is {typical:.1f} px (hand in view?). "
                  f"NOT recorded: move away, then ENTER again.")
        elif marks[i] < 100 and hpx >= 0.97 * h:
            print(f"  !! reads the top of the box at only {marks[i]:g}% (hand in view? box too short?). "
                  f"NOT recorded: move away, then ENTER again.")
        else:
            curve.append((hpx, marks[i] / 100.0))
            print(f"  {marks[i]:g}% -> {hpx:.1f} px  recorded")
            i += 1
            if i < len(marks):
                print(f"  -> fill to {marks[i]:g}%")
    if len(curve) > 1:
        print(f"  {name}: {len(curve) - 1} marks, full = {curve[-1][0] / curve[-1][1]:.1f} px")
    return curve


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


def click_boxes(cam, names, empty=()):
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
        what = f"EMPTY column {col} (any order)" if col in empty else f"Column {col}"
        f, _ = cam.read()
        for c in clicks[len(boxes) * 2:]:
            cv2.circle(f, c, 4, (0, 0, 255), -1)
        show(f, [f"{what}: click {which}", "right-click = undo"], boxes)
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
    p.add_argument("--empty", type=int, default=8,
                   help="how many columns must stay empty (watched, named X1..)")
    p.add_argument("--each", action="store_true", help="calibrate every column separately")
    p.add_argument("--syringe", default="A", help="column that gets the syringe steps (the others copy it)")
    p.add_argument("--percent", action="store_true",
                   help="calibrate by how full the column is (fill to each --marks by eye) instead of mL steps")
    p.add_argument("--marks", default="25,50,75,100", help="--percent: fill marks, %% of full")
    p.add_argument("--capacity-ml", type=float, default=4.5,
                   help="--percent: mL in a FULL column, so readings can still be given in mL")
    p.add_argument("--out", default="level_calib.json")
    a = p.parse_args()
    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    names = NAMES[:a.columns]
    empty = [f"X{i + 1}" for i in range(a.empty)]

    # Step 1: boxes by mouse
    print("STEP 1: click in the CAMERA WINDOW: top-left then bottom-right for each column.")
    print(f"        {', '.join(names)} first, then the {len(empty)} empty columns in any order.")
    print("        (If you can't see the window, look behind Terminal.)")
    columns = click_boxes(cam, names + empty, set(empty))
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
    if a.syringe not in names:
        raise SystemExit(f"--syringe {a.syringe} is not one of the columns {', '.join(names)}")
    targets = names if a.each else [a.syringe]
    fill_curves = {}
    for name in targets:
        col = columns[name]
        if a.percent:
            marks = [float(m) for m in a.marks.split(",")]
            fill_curves[name] = percent_steps(cam, q, col, ref, name, marks)
            col.curve = [(px, frac * a.capacity_ml) for px, frac in fill_curves[name]]
            continue
        curve, ml = [(0.0, 0.0)], 0.0
        print(f"\nSTEP 3, column {name}: add {a.step} mL, move your hand AWAY, press ENTER "
              f"(it waits for the level to hold still).\n"
              f"        r + ENTER = measure the same amount again (after a bad reading, no new liquid).\n"
              f"        n + ENTER = finished.")
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
            retry = cmd == "r"
            if retry and ml == 0:
                print("  nothing to measure again yet: add liquid and press ENTER")
                continue
            if not retry:
                ml += a.step
            if retry and curve[-1][1] == ml:      # re-measure the last recorded amount: replace it
                curve.pop()
            print(f"  {ml:.2f} mL: waiting for the level to hold still ...")
            hpx, why = steady_height(cam, col, ref)
            if why:
                print(f"  !! {why}. Point NOT recorded: move away, then r + ENTER.")
                continue
            prev = curve[-1][0]
            steps = [b[0] - a_[0] for a_, b in zip(curve, curve[1:])]
            typical = float(np.median(steps)) if steps else None
            if hpx <= prev:
                print(f"  !! {hpx:.1f} px is not above the last point ({prev:.1f} px). Point NOT recorded: "
                      f"check the box and backlight, then r + ENTER.")
            elif typical and hpx - prev > 2.5 * typical:
                print(f"  !! jumped {hpx - prev:.1f} px, usual step is {typical:.1f} px (hand in view?). "
                      f"Point NOT recorded: move away, then r + ENTER.")
            elif hpx >= 0.97 * h and len(curve) < 3:
                print(f"  !! reads the top of the box after only {ml:.2f} mL (hand in view?). "
                      f"Point NOT recorded: move away, then r + ENTER.")
            else:
                curve.append((hpx, ml))
                print(f"  {ml:.2f} mL -> {hpx:.1f} px  recorded ({len(curve) - 1} points)")
        col.curve = curve
        if len(curve) > 1:
            print(f"  {name}: {len(curve) - 1} points, about "
                  f"{curve[-1][0] / curve[-1][1]:.1f} px per mL")

    for name, col in columns.items():          # same geometry: copy A's table
        if name not in targets:
            col.curve = list(columns[targets[0]].curve)
            if a.percent:
                fill_curves[name] = list(fill_curves[targets[0]])
    extra = {"step_ml": a.step, "ref_image": "level_ref.png", "empty": empty}
    if a.percent:
        # fill_curve: [[height_px, fraction of full], ...] per column; curve stays px -> mL (fraction x capacity)
        extra.update(fill_curve={k: [list(p) for p in v] for k, v in fill_curves.items()},
                     capacity_ml=a.capacity_ml)
    save_columns(a.out, columns, extra)
    print(f"\nSaved {a.out}. Check it live with:  python level.py --cam {a.cam}")
    cv2.destroyAllWindows()
    cam.stop()


if __name__ == "__main__":
    main()
