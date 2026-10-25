# 07 - Open problems and the plan

## 1. The newly taught pour poses don't line up with the containers (check FIRST)
Along the tray line, relative to the tray centre: container markers a -45.8, b -15.1, c +15.5, d +45.4 mm; taught
gripper tips a **+2.8**, b -6.5, c +21.0 mm. a's tip is further toward d than b's, and a/b differ by only 9 mm along
the tray (mostly across the tray and in height instead), with near-identical headings. Either the vial was over a
different container than intended for some letters, or the vial sits far behind the tip and was held differently.
The arm only reproduces what was taught. **Next step that was about to run:** `python pour_episode.py C2a --run
--dry-tilt --max-tilt 150` (empty vial C) so the user can see where the vial actually ends up over container a; then
re-teach any wrong pose (quickly - J2 overheats) and relearn offsets with the tray unmoved. d still needs re-teaching.

## 2. Aim accuracy when the tray moves (the user's main requirement)
Root cause: `markers.py` maps the scan photo to the table with ONE homography fitted from touches at 2 tray
positions; away from those it's ~10 mm (= 1 channel) off. Offsets are already marker-relative; the error is in where
the marker is believed to be. Options:
* **a. Wrist-camera alignment (`servo.py`, built)**: needs references. At the pour pose the camera looks horizontally
  (markers not visible), so define a **look pose relative to the pour pose** (e.g. +100-150 mm up and pitched so the
  camera looks down at the markers), save the markers' pixel positions there at teaching time, and at run time go to
  the same relative look pose above the PREDICTED pour pose, align (sideways moves until the pixels match; Jacobian by
  +-6 mm test moves), then apply the same correction to the pour pose before lowering. Teaching can be automatic: after
  the user teaches the pour poses (tray unmoved), the arm visits each, rises, searches pitch angles until 2+ markers
  are in view, saves the reference. Rest the arm between containers (J2 heat).
* b. Better map: lens calibration with a checkerboard (vision.py `calib-lens` / the printed `vision_markers.pdf` has a
  checkerboard page) + more touch/shape positions spread over the area (`markers.py calibrate ...`). The user dislikes
  touch sessions.
* c. In-run correction from the level camera (built, `pour_episode` aim retry): costs ~0.5-1.5 mL in the wrong
  channel per correction; the stream can also miss the reservoir entirely (no correction possible).
* handeye.py (full camera model) failed: needs a real calibration target.

## 3. vial B spills outside the reservoir
Per-vial grip differences. Fix by teaching vial B's pour pose at b (teleop) and storing the difference to vial A's as
its `vial_offsets.yaml` entry (along AND across the tray); same for C and D.

## 4. The pour empties a conical tube in ~1 s once it starts
At ~100 deg the whole vial dumps; fill-rate control can't throttle it. Use less liquid per vial, or slow the tilt
near the onset more (search_slow_deg_s), or stop at the first sign of flow for small targets.

## 5. Hardware reliability
* CAN adapter: plug directly into the laptop; replug when it disappears / says Access denied.
* Gripper limp: always open/close check before an episode (target 20 -> ~19).
* J2 heat: don't hold stretched poses long; check `max_temp_c` in arm_status.json.
* Leader drift: recalibrate at rest when teleop refuses or the gripper won't follow.
* Battery: plug in the charger.

## 6. Smaller things
* Level model reads 1-2 mL of glare on the far-left edge column (x1); mitigated by `watch_near=2`.
* `vial_B` correction (-10.2) predates the re-taught b pose; re-check.
* The side camera must stay fixed relative to the tray for the level boxes to stay valid (it appears to move with it).
* `pour_episode` restarts live_view only on a clean exit; after a hard kill, restart it by hand (04).

## What the user wants next (in order)
1. CAN adapter back -> C2a dry run -> confirm/re-teach pour poses (incl. d) -> relearn offsets.
2. Make A2b and B2b perfect with the tray anywhere (finish 2a + 3).
3. Then `A2b:50 B2b:+50` and other mixes, with reports after each run.
