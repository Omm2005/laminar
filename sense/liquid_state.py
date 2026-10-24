"""Write the side webcam's view of every column to a JSON file for the arm, ~10x a second.

For each column: how full it is, the mL estimate, the liquid colour, and whether the
camera trusts the reading. The file is replaced in one step (write temp file, rename),
so the arm never reads a half-written file.

liquid_state.json looks like:

    {
      "ts": 1761262800.12,             # time.time() of the newest camera frame
      "time": "2026-10-23T23:40:00",
      "columns": {
        "A": {"ml": 0.42, "fill": 0.36, "px": 41.8, "conf": 1.0, "trusted": true,
              "color": "blue", "rgb": [40, 90, 210]},
        "B": {"ml": 0.0, "fill": 0.0, "px": 0.0, "conf": 1.0, "trusted": true,
              "color": "none", "rgb": null},
        ...
      }
    }

  fill   = liquid height / column box height (1.0 = box top, just below the rim)
  color  = red, orange, yellow, green, cyan, blue, purple, pink, clear, dark,
           or none (too little liquid to see)
  trusted = conf >= 0.5. Don't act on a column that isn't trusted.
  role    = "target" (A..D, liquid goes here) or "empty" (X1..X8, must stay empty)

  liquid_in_empty = names of empty columns where the camera sees liquid (trusted and more
  than --empty-ml). Not empty = liquid went somewhere it shouldn't: stop the arm.

How the arm code reads it:

    import json, time
    state = json.load(open("liquid_state.json"))
    if time.time() - state["ts"] > 0.5:
        ...                                   # camera script stopped: don't trust it
    a = state["columns"]["A"]
    if a["trusted"] and a["color"] == "blue":
        ...
    if state["liquid_in_empty"]:
        arm.tilt_back()                       # liquid where it shouldn't be

Run:  python liquid_state.py --cam 0
      python liquid_state.py --cam 0 --out /path/the/arm/reads.json --no-window
"""
from __future__ import annotations

import argparse
import json
import os
import time

import cv2

from cameras import LatestFrame, open_camera
from level import LevelTracker, draw_overlay, load_columns, load_empty, load_reference


def snapshot(tracker: LevelTracker, empty=(), empty_ml: float = 0.3) -> dict:
    cols, newest, wet = {}, 0.0, []
    for name, col in tracker.columns.items():
        s = tracker.read(name)
        newest = max(newest, s.ts)
        cols[name] = {"role": "empty" if name in empty else "target",
                      "ml": round(s.ml, 3), "fill": round(s.px / col.roi[3], 4),
                      "px": round(s.px, 1), "conf": round(s.conf, 2),
                      "trusted": s.conf >= 0.5, "color": s.color,
                      "rgb": list(s.rgb) if s.rgb else None}
        if name in empty and s.conf >= 0.5 and s.ml > empty_ml:
            wet.append(name)
    return {"ts": round(newest, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest)),
            "liquid_in_empty": wet,
            "columns": cols}


def write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)                       # atomic: readers see old or new, never half


def main():
    p = argparse.ArgumentParser(description="Write column levels + liquid colour to JSON")
    p.add_argument("--cam", default="0")
    p.add_argument("--calib", default="level_calib.json")
    p.add_argument("--out", default="liquid_state.json")
    p.add_argument("--hz", type=float, default=10.0, help="how often to rewrite the file")
    p.add_argument("--rezero", action="store_true",
                   help="capture a new empty reference instead of the one from calibration")
    p.add_argument("--no-window", action="store_true", help="don't show the camera window")
    p.add_argument("--empty-ml", type=float, default=0.3,
                   help="liquid above this in an empty column counts as liquid_in_empty")
    a = p.parse_args()

    cam = LatestFrame(open_camera(int(a.cam) if a.cam.isdigit() else a.cam)).start()
    cols = load_columns(a.calib)
    empty = load_empty(a.calib)
    tr = LevelTracker(cam, cols)
    ref = None if a.rezero else load_reference(a.calib, cam.read()[0].shape)
    if ref is not None:
        tr.ref = ref
        print("Using the empty reference from calibration. Use --rezero if the light moved.")
    else:
        input("Empty the columns, then press ENTER to capture the empty reference...")
        tr.zero()
    tr.start()
    time.sleep(0.3)
    print(f"Writing {a.out} {a.hz:g}x a second. Ctrl+C to stop.")

    win = "liquid state (Ctrl+C in Terminal to stop)"
    period, last_print = 1.0 / a.hz, 0.0
    try:
        while True:
            t0 = time.time()
            state = snapshot(tr, empty, a.empty_ml)
            write_json(a.out, state)
            if t0 - last_print > 1.0:
                last_print = t0
                print("  " + "  ".join(f"{k}: {c['ml']:.2f} mL {c['color']}"
                                       + ("" if c["trusted"] else " (?)")
                                       for k, c in state["columns"].items()
                                       if c["role"] == "target")
                      + (f"  LIQUID IN EMPTY: {', '.join(state['liquid_in_empty'])}"
                         if state["liquid_in_empty"] else ""))
            if not a.no_window:
                f, _ = cam.read()
                cv2.imshow(win, draw_overlay(f, cols, tr, empty))
                cv2.waitKey(1)
            time.sleep(max(0.0, period - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        tr.stop()
        cv2.destroyAllWindows()
        cam.stop()


if __name__ == "__main__":
    main()
