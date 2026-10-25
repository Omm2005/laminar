# 05 - History (what was done, in order)

## Oct 23-24 (before tonight)
* Set up teleop (StarArm leader -> reBot follower) on Windows, recording at 20 fps with the wrist camera.
* Code moved into `robotics/` in Omm2005/laminar. arm_server/arm_client, motion planner (lift/travel/lower with IK),
  episode replay (recorded rest->A then planned A->B and back), gripper fixes (stall logic, run-mode re-enable).
* Fetch.ai entry agent `laminar-lab` + planner agent, Tiger Data hypertables, console (Convex) work by teammates.
* `vision.py` ArUco container finder; later `markers.py` (tray finder with homography, calibrated by touching markers
  at 2 tray positions) and `deliver.py` (vial -> container found by its marker).
* Recorded vial paths A-D and initial positions; taught 4 final positions; learned container offsets at the
  "original" tray place; marker-relative pour poses with closest-reachable orientation; plan_turn; sag correction;
  sample tilt; level-model integration (`pour_episode.py`).
* Oct 24 22:40: the arm fell on a CAN dropout -> fixed so the arm never drops (b28029f).

## Oct 25 night (this session, newest at the bottom)
* 00:00-00:43 A2b runs with the level model: no liquid came out - vial empty, then "no flow at max tilt (~80 deg)".
  Root cause: **J6 wrist roll can only tip ~81 deg**; a conical tube needs 100-120 deg.
* Vial A re-taught several times by teleop (final `vial_A_20261025_004942`, 28 s). Every lower made 40 mm deeper.
* **All-joint pour tilt** (`pour_tilt.py`, tip locked, IK table) - dry run reached 123 deg, later 139 deg.
* "Diabolically shaky" -> analysed `arm_trace.jsonl`: 25.8 Hz oscillation on J3/J4 (~1 deg p-p) = the 25 Hz joint
  streaming; fixed by `table_tilt` (send table once, stream the angle, server interpolates at 100 Hz) - `0fea28f`.
  "Lower then climbs back" = sag correction -> lower now stops short by the measured sag (`f2588b2`, 70 % share).
* Merged Om's `om/pour-fixes` (waits at max tilt, slower search, median/smoothed rate) with a calmer wrong-column alarm.
* 01:47 first real pour: liquid came out but landed **one channel left of B**; the wrong-column check missed it
  (it ran after the target's own confidence check; fixed - wrong column is step 0, fast-rise path) -> `3d2a271`.
  b's pour point shifted +10.2 mm (one channel) along the tray.
* **02:01 A2b: SUCCESS** - B 2 % -> 75 % (3.4 mL), stopped by the wrong-column check when x4 began to rise (+0.52 mL).
  Raw feeds saved as `Downloads\demo vid for msft track - A2b 2026-10-25 0201\` (`wrist cam.mp4`, `tray cam.mp4`).
* 02:12 C2c dry run stopped by a status-file read race (fixed `0792cf8`); vial C returned by hand-written recovery.
* Dashboard streaming added by a teammate (`publish.py`); pour_episode now shares cameras (`5ad8ea5`).
* 02:25 B2b: landed one channel RIGHT of B and **most of vial B spilled outside the reservoir** (the stream hit the
  tray's front edge near the marker strip; seen in wrist frames; the level camera cannot see outside the columns).
  Added `vial_offsets.yaml` (vial B -10.2 mm along the tray). Across-tray error for vial B NOT fixed.
* Fill targets + sequences (`A2b:50 B2b:+50`), 100 % = 95 % of the box (`557104b`). Level display damping (`425eaf3`).
* 02:48-03:00 hardware trouble: gripper limp (mode 2, no fault, 0 torque) - came back after a direct enable;
  arm_server "lost feedback" at startup (race, fixed `1aa1dff`); CAN adapter wedged by a bring-up script that opened
  it twice (script deleted at the user's request); adapter unplugged/replugged several times.
* 03:03 `A2b:50`: landed one channel LEFT of B (x3 2.4 mL, B ~0.7-1.1 mL); stopped before B2b. Cause: **the tray had
  moved ~53 mm** since the 02:01 run; the homography map is ~10 mm off there. User: "fix it via offsets from the
  markers, the tray will keep moving, no taping".
* Aim correction from the level camera (move one channel the other way and pour on, `c76c4dc`).
* handeye.py collected 30 views (19 usable) - lens/hand-eye solve failed (geometry too small).
* servo.py (wrist-camera alignment) written and hooked into deliver (`de34b35`), not yet usable: no references.
* 03:3x teleop teaching of pour poses with vial A: a, b, c saved; **J2 hit 81 C**, teleop stopped before d.
* 03:38 offsets for a, b, c relearned from the scan-pose photo `logs/handeye_20261025_032235/12.jpg` (tray at
  (415.9, 339.1) heading -2.5 deg, fit 1.5 mm); d kept old (`b078b99`). Round-trip check: offsets reproduce the taught
  joints exactly. BUT the taught tips are inconsistent with the container spacing (see 07).
* CAN adapter missing again at ~03:50; the user asked for this hand-off before the C2a dry run.
