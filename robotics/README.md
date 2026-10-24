# Robotics — Avery
arm.py (MotorBridge driver), IK, S-curve, pose-save script, bench.yaml, pick/carry/place, pour controller, abort handling.

Windows follower driver, leader-arm teleop, recording and the scripted arm server (Amogh): see [TELEOP.md](TELEOP.md).
## Shared moves: lift, lower, travel

Every task is: recorded grab → `lift(100)` → shift `(dx, dy)` in that locked horizontal plane → `lower(60)` →
adaptive pour → `lift(60)` → shift `(-dx, -dy)` → `lower(100)` → grab played backwards. The moves in between
are computed here, not recorded, because a recording only works from the exact pose it was recorded at.
The only bench measurements are the 2-D distances from each vial to each compartment, in `layout.yaml`.

### Vial → compartment → vial in one command: `transfer.py`

```
cd software && python -m laminar.pour_motor6 calibrate --cam 2   # once: side camera, column boxes, % marks
cd robotics
python transfer.py vial_A well_2                              # plan the six moves and print them
python transfer.py vial_A well_2 --cam 2 --run                # run, camera pour to 90 % of full
python transfer.py vial_A well_2 --pour fixed --tilt 80 --hold 3 --run   # no camera: fixed tilt
```

All six moves are planned (and safety-checked), and the camera calibration and camera are checked, before
anything moves; each move is played continuously. The pour is Om's `pour_right` / `pour_left`
(`software/laminar/pour_motor6.py`): it fills the compartment's camera column (`pour.columns` in
`layout.yaml`) to `--target` of full, slowing as it fills, and tilts back upright when done or on wrong
column, overfill, camera lost or no flow. It streams the wrist roll through arm_server's `tilt_stream` op
with a heartbeat; if updates stop for 0.5 s arm_server tilts the tube back upright on its own. The roll
never goes past its soft limit (�90�), and the plan refuses a pour with under 20� of roll room.
`pour.direction` in `layout.yaml` (or `--side`) sets which way the roll tips the lip toward the
compartment. After any pour result the tube is carried back; only an arm error stops it where it is.
The return trip ends where the grab ended, within 1 mm.

| File | What it does |
| --- | --- |
| `motion.py` | Plans straight-line moves with IK: `plan_lift`, `plan_lower`, `plan_shift`, `plan_travel`, `plan_transfer` / `plan_return`, `plan_sequence`. Pure math, never moves the arm. |
| `arm_client.py` | Sends a planned path to `arm_server.py` (`mode="path"`, continuous, default; `mode="moves"` fallback) and streams live pour-roll targets. |
| `layout.yaml` | 2-D offsets (mm) from each vial to each compartment, lift/lower heights, pour roll direction. |
| `transfer.py` | The full round trip with the camera pour (above). |
| `spots.py` / `spots.yaml` | Taught poses: `slot_A..D` (where each grab ends) and `well_1..4` (pour-ready pose over each well). |
| `move.py` | CLI: plan, run, teach spots. |
| `tests/` | `test_motion.py`: straightness, height, orientation, speed caps, refusals. `test_transfer.py`: flat shifts, round trip back onto the grab pose, layout parsing, roll limit, pour_motor6 on the tilt stream with simulated liquid. `python -m pytest -q tests` |

Units: joint angles in **degrees** `{1..6}` in the follower frame (same as `arm_server.py` and recordings),
positions in **mm** in the arm base frame, speeds in mm/s. Forward kinematics comes from `kinematics.py`.

### How a move is planned

1. FK of the current joints gives the start position and orientation.
2. The straight line to the end point is cut into 1 mm samples.
3. IK solves each sample, seeded from the previous one so the arm never flips configuration.
4. Every sample is checked: IK error ≤ 1 mm / 2°, joint limits, no joint jumping > 3° per mm, workspace box.
   Any failure raises `PlanError` and nothing moves.
5. A minimum-jerk speed profile (gentle start and stop) is sampled at 50 Hz. If a joint would exceed
   45 °/s the whole move is slowed down.

Lift, lower and shift keep the gripper's orientation exactly (shift is a pure translation). Travel keeps
the height exactly and turns only about vertical, so an upright tube stays upright; a target that would
tilt the tube is refused.

### Use

Start `arm_server.py` first (see [TELEOP.md](TELEOP.md)). Everything plans and prints unless you add `--run`.

```
cd robotics
python move.py from-recording slot_A recordings/<grab_A run>/data.csv   # slot_A = last pose of grab A
python move.py teach well_1            # jog the arm (tube in hand, upright, lowered over well 1), then save
python move.py lift 100                # plan only
python move.py lift 100 --run          # move
python move.py travel well_1 --lower-next 60 --run
python move.py lower 60 --run
```

From Python:

```python
import motion as M
from arm_client import ArmClient
from spots import get_spot

arm = ArmClient()
q = arm.current_joints()
paths = M.plan_sequence(q, [("lift", 100), ("travel", get_spot("well_2")), ("lower", 60)])  # all planned before anything moves
for p in paths:
    arm.run(p)          # continuous ("path" mode)
```

### Heights must agree

Travel never changes height, so `lift(100)` from a slot then `lower(60)` reaches a well only if the well's
taught pose is 40 mm higher than the slot pose. `--lower-next 60` (or `height_offset_mm=60`) prints the
mismatch; fix it by re-teaching the well pose or changing the lower distance.

### Continuous playback: arm_server.py `path` and `tilt_stream` ops

`{"op": "path", "points": [...], "hz": 50}` plays a planned Path continuously, interpolating between points at
the 100 Hz control rate, with no easing between them. It is refused unless the first point is within 2° of the
current command and no step needs more than 90 °/s. `{"op": "tilt_stream", "joint": 6, "base": deg}` follows
live offsets from `arm_tilt.json` (rate-limited, clamped to the joint limits) until told `done` or until
updates stop, then returns the joint to `base`. `mode="moves"` is still there as a fallback.

### Tuning knobs (top of `motion.py`)

`DEFAULT_LIFT_SPEED` 20 mm/s, `DEFAULT_LOWER_SPEED` 15 mm/s, `DEFAULT_TRAVEL_SPEED` 30 mm/s,
`MAX_JOINT_SPEED_DEG_S` 45, `POS_TOL_MM` 1, `ANG_TOL_DEG` 2. All are first guesses; tune on the arm.

## Vision: finding the containers

`vision.py` finds the 4 pour containers wherever the judges put them, with the wrist camera. Each container sits on a
flat card with its own ArUco marker (DICT_4X4_50, ids 10-13 = containers 1-4, 40 mm) lying flat beside it. The arm
goes to one fixed **scan pose** (high, camera straight down); there the camera is effectively a fixed overhead camera,
so pixels map to the table through a homography, calibrated once by touching the 4 markers with the gripper tip. If a
marker is out of view, only the base (J1) sweeps (-40, -20, 0, +20, +40 deg) and the result is rotated back about the
base axis. Pour spots are taught relative to each container's own marker, so they move and turn with the container.
Scan -> compute -> move: nothing is tracked while the arm moves, and every target is checked against the workspace box,
the tube rack and the other containers. The tubes stay in their fixed rack; nothing is touched during a normal run.

Install: `pip install -r requirements.txt` (`opencv-contrib-python` provides `cv2.aruco`; uninstall `opencv-python`
first if it is installed, the two conflict).

### One-time setup

```
python vision.py markers                  # vision_markers.pdf: print at 100 %, check a marker measures 40 mm
python vision.py calib-lens photos/       # optional: 10-20 photos of the checkerboard page from the wrist camera
python vision.py calib-table --run        # guided: jog to the scan pose (Enter), spread the containers to the
                                          # corners of their area, touch each marker centre with the gripper tip
                                          # (Enter x4), then it returns to the scan pose and takes the photo
python vision.py check-sweep --run        # optional: same marker seen at two base angles should agree < 2 mm
python vision.py teach-well well_1 --run  # per container: scan, then jog to the pour pose (tube held, tilted to a
                                          # typical pour angle, lip over the container) and press Enter
```

Redo `calib-table` only if the camera mount or the scan pose changes. Optional `vision:` keys in `bench.yaml` override
the defaults at the top of `vision.py`, e.g. `marker_mm`, `camera`, `sweep_stops_deg`, `container_radius_mm`,
`tube_rack_mm: {min: [x, y], max: [x, y]}` (spots inside the rack rectangle are refused; without it the rack isn't
checked).

### A run

```
python vision.py scan --run               # all 4 container poses + their pour targets
python vision.py show                     # vision_debug.jpg: markers, ids, pour spots drawn
```

In code, after the recorded grab of a tube:

```python
import motion as M, vision as V
from arm_client import ArmClient

client = ArmClient()
dets = V.scan(client, run=True)                              # do this BEFORE the grab: the scan pose is high
# ... play the recorded grab of the tube ...
q = client.current_joints()
lift = M.plan_lift(q, 100)
_, R_carry = M.fk_mm(lift.final_joints)                      # the tube's carry orientation
target = V.well_target("well_2", dets, R_carry=R_carry)      # {"xy_mm": [x, y], "R": 3x3}, safety-checked
travel = M.plan_travel(lift.final_joints, target)
lower = M.plan_lower(travel.final_joints, 60)
for p in (lift, travel, lower):
    client.run(p)
# ... pour (wrist roll), then the reverse: lift(60) -> travel back to the slot -> lower(100) -> release
```

| File | What it does |
| --- | --- |
| `vision.py` | Lens + table calibration, ArUco detection, base sweep, marker-relative pour spots, CLI |
| `vision_calib.yaml` | Lens model + homography + scan pose (local, not committed) |
| `tests/test_vision.py` | Synthetic marker images: homography, detection, rotations, sweep consistency, safety checks |
