# 03 - Code map

Repo `Omm2005/laminar`, branch `main`, working copy `C:\Users\amogh\Downloads\laminar-main`. Python 3.10 at
`C:\Users\amogh\AppData\Local\Programs\Python\Python310\python.exe`. Tests: `cd robotics && python -m pytest -q`
(73 pass) and `cd software && python -m pytest -q` (141 pass, 4 skipped).

## robotics/ - the arm
| File | What it does |
| --- | --- |
| `arm_server.py` | Owns the CAN bus. Holds the arm at 100 Hz, executes one command at a time from `arm_cmd.json` (ops: `move`, `gripper`, `grasp`, `path`, `tilt_stream`, `pose_stream`, `table_tilt`, `rest`/`stop`). Writes `arm_status.json` (commanded + measured joints, gripper pos/torque/mode/fault, temps, CAN errors). Gravity+friction feedforward from `compensation.json`. Lost feedback -> holds 2 s, never disables mid-air. Re-enables motors that leave run mode. 1 s startup grace for first replies. Exits after 15 min idle or on `rest`. |
| `arm_client.py` | `ArmClient`: `current_joints()` (commanded), `status()` (retries a racy read), `_send(cmd)`, `run(Path)`, tilt/pose/table-tilt stream helpers. |
| `arm_cmd.py` | `python arm_cmd.py rest` etc. from the shell. |
| `motion.py` | FK/IK (`fk_mm`, `ik`), Cartesian planning: `plan_line`, `plan_lift`, `plan_lower`, `plan_travel` (flat, turns only about vertical), `plan_turn` (orientation change in place), joint limits, transfer heights **TRANSFER_LIFT_MM = 150, TRANSFER_LOWER_MM = 150** (pour pose at the grab height). |
| `episode.py` | `plan_a_to_b` (lift / flat travel / lower between two taught poses), `reverse_path`; **EXTRA_LOWER_MM = 40** (every final lower goes 40 mm deeper than the taught/target height). |
| `teleop.py` | Leader -> follower teleop at 100 Hz (`--hz 100 --keep-grip --smooth-ms 100`), optional `--record --fps 20 --camera 1`. Stop gracefully by creating `robotics/STOP` (writes the MP4 properly, returns to rest). Prints `J1..J7` (commanded follower) + `grip leader X jaws Y` + temps + Hz. |
| `grab.py` | Save / play / transfer labelled teleop motions: `save vial_X --rec <rec> --frame N` writes `paths/vial_X.py` and `vial_X_initial_position`; `play vial_X [--reverse] --run`; `transfer vial_X --to <spot> --run`. |
| `paths/vial_A..D.py` | Recorded grab motions (rest -> vial in its rack slot, gripper closes). Current: A 461 pts/28.1 s (source `vial_A_20261025_004942`), B 376/18.8 s, C 540/26.9 s, D 500/26.6 s. |
| `spots.yaml` + `spots.py` | Named poses. Group `vials`: `vial_X_initial_position` (grasp pose), `vial_X_final_position` (pour pose over container x, taught by teleop). |
| `markers.py` | Tray finder: scan pose -> wrist photo -> ArUco -> **homography** (pixel -> table mm, fitted from touches at 2 tray positions, `vision_calib.yaml` `tray` section, local) -> rigid fit of the marker layout -> tray pose `{xy, heading_deg, containers{1..4: xy}}`. `locate_tray(client, calib, camera)` drives the scan. `settle_on` = encoder sag correction. Accuracy ~3-4 mm near calibrated spots, ~10 mm elsewhere: **the root of the aim problem**. |
| `deliver.py` | One job `A2b`: find tray -> grab (play path) -> lift -> travel -> lower -> turn into pour pose -> sag correction -> [wrist align] -> pour hook -> undo -> reverse moves -> release. Pour pose = container marker + offset (`container_offsets.yaml`, tray frame) + vial correction (`vial_offsets.yaml`); `reachable_pour` gives the nearest reachable orientation if J4 limits it; lower stops short by 70 % of the carry-height sag (`SAG_PRECOMP`). `learn-offsets --run` scans and stores each `vial_X_final_position` in its marker's frame. `HOOKS` = phase / pour callbacks for runners. |
| `pour_episode.py` | **The episode runner.** `python pour_episode.py A2b:50 B2b:+50 --run [--dry-tilt] [--max-tilt 140] [--pour-mode tip|roll]`. Shares cameras with the dashboard (reuses a running wrist_stream, borrows the side camera from live_view and restarts it after). Logs everything to `logs/episode_<jobs>_<time>/` (`levels.csv` raw+damped per column at 10 Hz, `side/`, `wrist/` frames at 2 Hz, `pour_guide.jsonl`, `summary.json`). Pour = all-joint tilt (below) driven by the vision model; **aim correction**: if a neighbour channel fills, tip back, move one channel the other way along the tray, pour on (2 retries). |
| `pour_tilt.py` | All-joint pour: IK table of J1-J6 per degree of tilt about a horizontal axis through the gripper tip (tip locked), best starting heading; `TipTiltJoint` (same interface as pour_motor6's joint) sends the table once (`table_tilt` op) then only the angle. Reaches 120-150 deg (J6 alone stops at ~81 deg, too little to empty a conical tube). |
| `servo.py` | **New, unfinished**: wrist-camera alignment to a taught reference (markers' pixel positions + image Jacobian). Hooked into deliver after the sag correction **if** `servo_refs.yaml` has the container. Problem: at the pour pose the wrist camera looks horizontally and cannot see the markers -> needs a "look pose" (see 07). |
| `handeye.py` | Camera intrinsics + hand-eye by bundle adjustment from ~30 viewpoints (`collect --run`, `solve`). Collected `logs/handeye_20261025_032235` (19 usable views) but the solve failed (26 px RMS): 4 small markers over ~90 mm are too little geometry for a lens fit. Kept for reference; a checkerboard would fix it. |
| `rest_arm.py` | Ease to rest without dropping (takes over holding motors), disable. `--from-recording`, `--keep-grip`. |
| `calibrate_leader.py` / `calibrate_follower.py` | Zero the leader at its rest pose / record the follower zero (passive). |
| `detect_arm.py` | Scan the bus: `7/7 motors found`. |
| `wrist_stream.py` | Owns the wrist camera, writes `wrist_latest.jpg` (10 fps) for everyone else. |
| `publish.py` | (teammate) Streams `arm_status.json`, `wrist_latest.jpg`, `side_latest.jpg` to the website (Convex). Read-only. |
| `vision.py`, `transfer.py`, `move.py`, `grab.py`, `episode.py`, `compensation.py`, `smooth_path.py`, `arm_trace.py` | older/other tools; arm_trace writes `logs/arm_trace.jsonl` (100 Hz cmd/pos/vel/torque - great for diagnosing shake/sag). |
| `container_offsets.yaml` | Per container: marker id, `dxy_mm` (tray frame), `z_mm`, `heading_offset_deg`, `R_tray`, taught joints. |
| `vial_offsets.yaml` | Per vial correction (tray frame). vial_B: [-10.2, 0]. |
| local only (gitignored) | `follower_calibration.json`, `leader_calibration.json`, `vision_calib.yaml`, `compensation.json`?, `recordings/`, `logs/`, `wrist_latest.jpg`, `side_latest.jpg`, `arm_status.json`, `arm_cmd.json`, `STOP`. |

## sense/ - the level model (Om)
* `level_seg.onnx` (+ `MODEL.md`): reads how full each column is from the side camera; `seg_level.SegLevelTracker`.
* `live_view.py`: draw boxes (`--new-boxes`), live levels, wrong-column warnings, writes `robotics/side_latest.jpg`.
  Run by the dashboard as `python -u live_view.py --cam 2 --no-record` (cwd `sense/`).
* `level.py`: `LevelTracker.read()` (raw, median of 3) and **`read_stable()`** (median 0.6 s, <0.3 mL -> 0, small
  flicker -> 0) for display/logs. The pour uses raw.
* `pour_guide.py`: `PourGuide` - fill/rate/stop/abort logic. **Wrong-column check runs first** (before the target's own
  confidence) and a fast rise (>3x threshold at conf >= 0.3) aborts after 0.1 s; `watch_near` = only N channels each
  side are watched.
* `cameras.py`: `open_camera(..., mode="ultrawide"|"wide")`.

## software/laminar/pour_motor6.py (Om, merged from om/pour-fixes)
The adaptive pour controller `_pour(joint, tracker, column, direction, cfg)`: search tilt (15 deg/s to 40, then 6),
onset hold, fill-rate schedule, wait 5 s at max tilt, stop at target with lag compensation, abort on wrong column /
overflow / camera lost / no flow. `PourConfig(target_frac, max_tilt_deg, wrong_column_ml, wrong_column_s, watch_near)`.
Returns `PourResult(result, reason, wrong_side ...)`.

## Other parts of the repo (not robotics)
`console/` (Convex web console, Hema), `laminar-web/` + `index.html` (website), `software/` (agents: Fetch.ai entry
agent `laminar-lab`, planner agent; Tiger Data telemetry `laminar/tiger.py`), `voice/`. Branches: `console`,
`console-agent`, `console-pour-fixes`, `tiger-runs`, `om/lab-agent`, `om/pour-fixes` (merged).
