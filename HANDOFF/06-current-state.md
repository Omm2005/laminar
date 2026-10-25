# 06 - Current state (2026-10-25 ~04:00)

## Physical
* Arm at rest, motors disabled. **CAN adapter not detected** (needs replug, directly into the laptop).
* Leader on COM18, calibrated at rest at ~03:35. Wrist camera = 1, side camera = 2.
* J2 was at 81 C at ~03:40 (cooling). Last read max temp 52 C.
* Laptop on battery (charger recommended).
* Tray: not moved since the 03:22 calibration sweep (as far as known), at about (416, 339) mm, heading -2.5 deg.
  Markers (homography): a (370.2, 341.0), b (400.8, 338.7), c (431.5, 339.5), d (461.3, 337.0).
* Reservoir contents: liquid left over from the 03:03 run (x3 ~2.4 mL, B ~1 mL) unless the user reset it.
* Dashboard processes: `live_view.py --cam 2 --no-record` and `wrist_stream.py --camera 1` were running; `publish.py`
  was not running at the last check.

## Saved numbers (all in git)
`spots.yaml` vials (J1..J6, degrees, follower frame):
| Spot | J1 | J2 | J3 | J4 | J5 | J6 |
| --- | --- | --- | --- | --- | --- | --- |
| vial_A_initial_position | 14.7 | 99.8 | 26.5 | 71.5 | -1.0 | 4.2 |
| vial_B_initial_position | 14.5 | 92.3 | 33.9 | 51.6 | -11.4 | 0.5 |
| vial_C_initial_position | 14.7 | 95.7 | 38.4 | 50.2 | -19.6 | 1.8 |
| vial_D_initial_position | 34.8 | 104.9 | 37.0 | 70.5 | 19.0 | -11.9 |
| vial_A_final_position (pour over a, 03:3x) | -57.8 | 131.7 | 65.8 | 90.0 | -63.7 | 7.7 |
| vial_B_final_position (over b, 03:3x) | -58.1 | 128.8 | 62.2 | 90.0 | -53.8 | 15.6 |
| vial_C_final_position (over c, 03:3x) | -54.1 | 126.3 | 63.4 | 84.1 | -51.7 | 8.6 |
| vial_D_final_position (over d, OLD, 02:3x) | -35.6 | 112.7 | 44.7 | 73.8 | -4.4 | -0.1 |

`container_offsets.yaml` (gripper tip relative to the container's marker, tray frame, x = along a->d):
| Container | marker | dxy_mm | z_mm | heading vs tray |
| --- | --- | --- | --- | --- |
| a | 10 | (+48.6, -14.6) | 215.0 | -5.4 deg |
| b | 11 | (+8.7, +12.2) | 230.7 | +4.5 deg |
| c | 12 | (+5.5, -6.1) | 242.3 | +3.0 deg |
| d (old) | 13 | (-6.0, +14.9) | 207.8 | +32.4 deg |
Pour pose = marker + rot(tray heading) * dxy (+ vial correction), height z - 40 mm (EXTRA_LOWER_MM), orientation =
taught orientation turned with the tray (nearest reachable if J4 limits it).

`vial_offsets.yaml`: vial_B (-10.2, 0) mm (from the 02:25 B2b; may be wrong now that b was re-taught).
`servo_refs.yaml`: does not exist yet (wrist-camera alignment inactive).

## What works (verified on the real arm)
* Teleop (after leader recalibration, on AC power for smoothness), recording, saving paths.
* Grabs of all 4 vials (gripper closes ~38-47 deg on a vial), carry with lift 150 mm, flat travel, lower 40 mm deeper,
  sag-compensated, return and release. Vial recovery after an interrupted run.
* Tray finding from the scan pose (all 4 markers, photos agree within ~0.1-0.6 mm).
* All-joint pour tilt to 120-150 deg with the tip locked; smooth (table_tilt, no 25 Hz shake).
* Level model on the side camera, live logs, wrong-column abort (fast), fill targets / sequences, dashboard sharing.
* A real pour into container b (02:01, 75 % full).

## What does NOT work yet
* Reliable aim with the tray moved (homography error ~1 channel).
* The 03:3x teaching looks inconsistent (07).
* vial B spills outside the reservoir (across-tray error for that vial).
* servo.py alignment has no references (wrist cam can't see markers at the pour pose).
* Hardware: CAN adapter keeps disappearing; gripper occasionally limp; J2 overheats on long holds.
