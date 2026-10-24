# Level model: setup and use

A small neural network (`level_seg.onnx`, 119k parameters) reads how full a reservoir column is from the
side camera. You draw a box over the column; it gives level, %, mL and fill rate live, and watches the
columns around it for spills. The arm uses it to pour (`pour_motor6 --boxes`): tilt more, slow down,
stop at the target, abort if liquid goes into the wrong column.

No calibration file needed: the box you draw is the calibration.

## 1. Setup (once)

```bash
git clone https://github.com/Omm2005/laminar.git && cd laminar
python3 -m venv .venv
source .venv/bin/activate
pip install -r sense/requirements.txt          # opencv, numpy, onnxruntime: enough to RUN the model
pip install torch                              # only to TRAIN / retrain it
```

The trained model is in the repo: `sense/level_seg.onnx` (+ `level_seg.json`, its test scores).

macOS: give your terminal camera access (System Settings > Privacy & Security > Camera), then restart it.

### Windows / Linux

The model, `live_view.py` and recording work the same everywhere. What changes:

| | macOS | Windows (PowerShell) | Linux |
| --- | --- | --- | --- |
| make the venv | `python3 -m venv .venv` | `py -m venv .venv` | `python3 -m venv .venv` |
| activate it | `source .venv/bin/activate` | `.venv\Scripts\activate` | `source .venv/bin/activate` |
| python in the commands below | `../.venv/bin/python` | `..\.venv\Scripts\python` | `../.venv/bin/python` |
| camera access | Privacy & Security > Camera for the terminal | Settings > Privacy > Camera, "let desktop apps" on | add yourself to the `video` group (`sudo usermod -aG video $USER`, log in again) |
| camera number | `python cameras.py` lists them | same (USB cameras are often 1 or 2; the laptop camera is 0) | same, or a fixed name: `--cam /dev/v4l/by-id/...` |
| start arm_server | `python mac_run.py arm_server.py` (macOS USB fixes) | `python arm_server.py` (the CAN driver was written on Windows) | `python arm_server.py`, untested; the USB-CAN adapter needs a udev rule or `sudo` |

With the venv activated, plain `python` works on all three (`python live_view.py ...`).
If `review.mp4` will not play on Windows/Linux, open it in VLC (it falls back to the mp4v codec there).

## 2. Test the model (camera only, no arm)

```bash
cd sense
python cameras.py                               # which index is the side camera?
python live_view.py --cam 0 --new-boxes
```

1. Click the camera window, drag ONE box over a column: top = rim, bottom = floor, sides just inside the walls. ENTER.
2. Pour by hand with a syringe: the % and mL should follow the liquid; empty should read ~0 %.
3. Squirt a little into a neighbour: it shows `WRONG COLUMN: liquid going LEFT/RIGHT of A`.

Keys (click the window first): `b` redraw boxes, `c` clear warnings, `q` quit (prints stats).
Next time drop `--new-boxes` to reuse the box (saved in `live_boxes.json`, local to your camera, not in git).
Redraw whenever the camera or tray moves.

Every live run is recorded to `seg_data/frames/live_<date>_<time>/` (`--no-record` to turn off).
Replay a recording instead of the camera: `python live_view.py --replay pour_20261024_112323 --new-boxes`.

## 3. Pour with the arm

```bash
# terminal 1: arm server, leave it running (it exits after `rest`, an error, or 15 min idle)
cd robotics && ../.venv/bin/python mac_run.py arm_server.py --hold-grip

# put the arm in pouring position: vial upright in the gripper, spout just above the target column

# terminal 2
cd software && ../.venv/bin/python -m laminar.pour_motor6 pour right --boxes --new-boxes --target 0.9
```

Draw ONE box over the target column, ENTER, then click the Terminal and type `y`.
`pour left` tilts the other way; `--target 0.9` = stop at 90 % full; `--max-tilt 60` limits the wrist;
`--col B` if you drew several boxes; `q` in the camera window stops the pour (the wrist tilts back).

What the arm does from the camera:

| Camera sees | Arm |
| --- | --- |
| nothing flowing | tilts more (15 deg/s) |
| filling slower than allowed for this fill level | tilts more, gently |
| filling too fast | slows down / tilts back |
| target close (fill x rate x 0.3 s lag) | tilts back upright: done |
| a neighbour rises, rim reached, camera unsure 1 s, no flow 2 s at max tilt | aborts, tilts back, says LEFT/RIGHT |

Motion only, no liquid: `pour right --sim --real-motor` (real wrist, simulated liquid).

Every pour is recorded to `sense/seg_data/frames/pour_<date>_<time>/`:
`review.mp4` (annotated video), `frame_*.jpg` + `times.csv` (raw frames), `pour.json` (boxes, result,
tilt / fill / rate / action at every step). `--review-cam 1` films a second camera next to it; `--no-record` turns it off.

## 4. Record, label, retrain

```bash
cd sense
python record_frames.py --cam 0 --session fill5 --fps 10     # SPACE pause, q stop
python label_new.py fill5          # drag ONE box over the whole tray (left wall of channel 1 to right wall of 12,
                                   # rim to floor), ENTER. --same-box reuses it, --check only redraws the overlays
python synth_dataset.py            # synthetic copies of the labelled frames
python -u train_seg.py             # ~1.5 h on an M-series Mac; prints a TEST report on recordings it never saw
```

Good data: strong dye, slow fills, empty AND full AND spill columns, different light, the arm/hand in view.
The pour and live recordings (`pour_*`, `live_*`) are the same format: label them the same way.
In git: each episode's `review.mp4`, `pour.json`, `times.csv`. The raw frames (`frame_*.jpg`) and the full
training set (`seg_data/`, ~540 MB) stay on Om's machine.

## Current model (2026-10-24)

Tested on two recordings it never saw (fill4, setup3), side-on camera view:

| Column | Mean error | 95 % within |
| --- | --- | --- |
| filled (>25 %) | 0.26 mL (5.8 %) | 0.94 mL |
| spill / little (2-25 %) | 0.08 mL | 0.19 mL |
| empty | 0.03 mL | 0.21 mL |

Spills caught 99 %; single empty readings that look like a spill 5.5 % (the pour needs 0.5 s of rise, so these don't stop it).
Not yet trained on the front view with the arm in frame: readings there are noisier. Record pours, label, retrain.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `no picture from the camera after 5 s` | another app has the camera (Zoom, Photo Booth, another script), wrong `--cam`, or no camera permission |
| ENTER / y does nothing | the camera window and the Terminal take keys separately: click the one you are typing into |
| `arm_server did not pick up ...` | arm_server is not running: start it (terminal 1) |
| `lost feedback from J1` | the computer was too busy: close training / heavy apps, draw only one box |
| `NOT STARTED: only N level readings a second` | same: needs 15 readings/s for the fill rate |
| `only N deg of room to the wrist limit` | the wrist is already rolled that way: `pour left`, or roll it back toward 0 |
| level line not on the liquid | box is off: redraw (`--new-boxes`), keep the camera and tray fixed |

## Files

| File | What it does |
| --- | --- |
| `seg_level.py` | runs the model: `SegLevel.measure(crop)`, `SegLevelTracker` (live, all boxes batched) |
| `live_view.py` | draw boxes, live levels, spill alarm, recording |
| `record_frames.py` | record training frames |
| `label_new.py` | auto-label a recording from one whole-tray box (spills included) |
| `autolabel.py`, `label_scene.py`, `label_levels.py` | labelling helpers / older labellers |
| `synth_dataset.py` | synthetic training copies |
| `train_seg.py` | train + test + export `level_seg.onnx` |
| `scene_level.py`, `train_scene.py` | whole-scene column finder (experimental, used by `pour_motor6 --auto`) |
| `../software/laminar/pour_motor6.py` | the camera-driven pour on motor 6 |
