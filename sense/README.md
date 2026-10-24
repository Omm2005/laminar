# Laminar vision starter code

**Level model (no calibration, draw a box, arm pours from it): see [MODEL.md](MODEL.md).**

```
pip install "opencv-python>=4.7" numpy
```

| File                 | Camera      | What it does                                                                                                                                        |
| -------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cameras.py`         | both        | Open a camera with locked settings; `LatestFrame` always gives the newest frame (no lag); `python cameras.py` lists cameras, `--cam N` previews one |
| `calibrate_level.py` | side webcam | Draw a box per column (A–D), then the 8 columns that must stay empty (X1–X8, liquid there stops a pour), capture the empty reference, add liquid by syringe in steps and press ENTER → `level_calib.json`                       |
| `level.py`           | side webcam | `LevelTracker`: mL per column at camera rate, `read("A")`, `flow("A")`; `FakeLevelTracker` for Om; `python level.py` = live view                    |
| `liquid_state.py`    | side webcam | Writes `liquid_state.json` ~10×/s for the arm: per column mL, fill, liquid colour, `trusted`; `python liquid_state.py --cam 0`                      |
| `wrist.py`           | arm camera  | Survey: slot occupied + cap colour; grasp check; column-under-spout check; spill check; AprilTag pixel→mm; `snapshot()` for Claude                  |

## Order to bring it up

1. `python cameras.py` → find both camera indexes (on Linux, use `/dev/v4l/by-id/...` names).
2. `python cameras.py --cam 0` → check ~30 fps at 640×480. Lock exposure.
3. Mount the side webcam facing the reservoir's long side, white light behind it, dyed water.
4. `python calibrate_level.py --cam 0 --step 0.5` → `level_calib.json`. Click A–D, then all 8 unused columns.
5. `python level.py --cam 0` → pour by hand with a syringe; does the reading match?
   Write down the error at 1, 2, 3, 5 mL. That number decides closed-loop vs recipe pour.
6. Arm camera: teach the survey pose, save an image of the EMPTY rack and of the
   empty gripper (references), then test `survey_rack`, `grasp_ok`, `spill_check`.

All thresholds are starting guesses; tune them on real images.
