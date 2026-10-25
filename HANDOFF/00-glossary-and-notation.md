# 00 - Glossary and notation (read this first)

Every name, letter, number and unit used across Laminar, what it stands for, and where it is defined. The same
physical thing has different names in different parts of the code (robotics, level model, website, agents); the
tables below line them up. Where two parts of the repo disagree, it says so under "Inconsistencies".

## The one rule to remember
**UPPERCASE A-D = what is poured (the liquids / vials / tubes). lowercase a-d = where it goes (the containers /
wells).** `A2b` = "vial A into container b".

## Liquids = vials = tubes (UPPERCASE A, B, C, D)
Four conical centrifuge tubes standing in a rack, left to right A, B, C, D. The arm picks one up, pours from it and
puts it back. "Vial" (robotics), "tube" (website), "chemical" / "liquid" / "sample" / "reagent" (people) all mean the
same thing.

| Letter | What it stands for | Demo liquid (colour) | Website label (`laminar-web/convex/bench.ts`) | Start volume (website) | Role in the assay |
| --- | --- | --- | --- | --- | --- |
| **A** | **Blood sample** | red-dyed water (`#c8213a`) | "Blood sample" | 5.0 mL | the sample: goes into every well first |
| **B** | Reagent 1 | blue (`#1f57d6`) | "Reagent 1" | 2.0 mL | tests the sample in well 1 |
| **C** | Reagent 2 | yellow (`#e0b416`) | "Reagent 2" | 2.0 mL | tests the sample in well 2 |
| **D** | Reagent 3 | green (`#1f9a55`) | "Reagent 3" | 2.0 mL | tests the sample in well 3 |

* In robotics code a vial is `vial_A` ... `vial_D` (`robotics/spots.yaml`, `robotics/paths/vial_X.py`).
* In words the website accepts colours for tubes: red / blood -> A, blue -> B, yellow -> C, green -> D
  (`laminar-web/convex/lib.ts`, `mana.ts`).
* The Mana Lab Console reports them as "red (sample A)", "blue (reagent B)", "yellow (reagent C)", "green (reagent D)".

## Containers = wells (lowercase a, b, c, d) on the 12-channel reservoir ("the tray")
The tray is a clear 12-channel reservoir. Only every third channel is used as a destination; the channels between
them are watched by the side camera to catch liquid going to the wrong place.

| Container | Website / assay name | Reservoir channel | ArUco marker id | `markers.py` / `deliver.py` index | `layout.yaml` name | Side-camera level box |
| --- | --- | --- | --- | --- | --- | --- |
| **a** | well 1 | channel 1 | 10 | 1 | `well_1` | `A` |
| **b** | well 2 | channel 4 | 11 | 2 | `well_2` | `B` |
| **c** | well 3 | channel 7 | 12 | 3 | `well_3` | `C` |
| **d** | well 4 (the **control** well) | channel 10 | 13 | 4 | `well_4` | `D` |

* **Careful: the side camera's level boxes are named A-D in UPPERCASE but they are CONTAINERS a-d** (`sense/live_view.py`
  names boxes left to right A, B, C, ...; `TARGETS = {1: "A", 4: "B", 7: "C", 10: "D"}` maps channel -> box). The
  watched in-between channels are `x1`, `x2`, ... `x9` (left to right). In `levels.csv` and the live view "A 0.40 mL"
  means "container a holds 0.40 mL", not vial A.
* ArUco: DICT_4X4_50, ids 10-13, 20 mm black squares on the tray's front edge, one per container, ~30.4 mm apart
  (3 channels x ~10.1 mm). Ids outside 10-13 are ignored.
* Channel numbers 1-12 run left to right as the side camera sees the tray.

## Job notation (robotics: `deliver.py`, `pour_episode.py`)
| Written | Means |
| --- | --- |
| `A2b` | vial **A** into container **b** (the "2" reads "to"). Case-insensitive; regex `[A-Da-d]2[A-Da-d]`. `deliver.parse("A2c") -> ("vial_A", 3)`. Any of the 16 pairs A-D x a-d is valid. |
| `A2b` (no fill) | fill container b to `--target` (default 0.9 = 90 % of full) |
| `A2b:50` | fill container b **to** 50 % of full (absolute) |
| `B2b:+50` | **add** 50 % of full on top of what the camera measures in b just before this pour |
| `A2b:50 B2b:+50` | a sequence: jobs run one after another (mix two liquids in one container by percent) |

* **"Full" = 95 % of the column's level box** (`TARGET_CAP = 0.95`, the user's rule so 100 % never overflows). Every
  percent is scaled by 0.95: 50 % = 47.5 % of the box.
* Volumes: a full column box is **4.5 mL** (`CAPACITY_ML`, level model calibration). Percent and mL are linear in the box.
* Flags: `--run` (required to move the real arm; without it you get a plan only), `--dry-tilt` (no vision: tilt to
  max and back, for an empty vial), `--max-tilt DEG` (default 140), `--pour-mode tip|roll` (default `tip`: all-joint
  tilt with the tip locked; `roll`: J6 wrist roll only).

## Episode phases (`deliver.py` `_phase`, shown on the side-camera window and logs)
`grab` (play the taught grab path, gripper closes) -> `lift` (150 mm up) -> `travel` (flat, at carry height) ->
`lower` (down to the pour height, 40 mm deeper than taught) -> `turn` (into the pour orientation) -> `settle` (encoder
sag correction) -> `align` (wrist-camera alignment, only if `servo_refs.yaml` has the container) -> `pour` (vision-
controlled tilt) -> `after pour` (tilt back) -> `return` (reverse the moves) -> `release` (grab path in reverse,
gripper opens) -> `done`. A dry tilt shows `dry tilt at max`.

## Website / assay vocabulary (`laminar-web/convex/lib.ts`, `bench.ts`, console)
* **Standard assay:** A 1.0 mL into wells 1, 2, 3, 4; then B 0.5 mL into well 1, C 0.5 mL into well 2, D 0.5 mL into
  well 3. **Well 4 is the control** (sample only, no reagent). In robot terms: A2a, A2b, A2c, A2d, B2a, C2b, D2c.
* Checker rules (plain code, decides whether a plan may run): well capacity 3.0 mL; one pour 0.2-2.0 mL; tube A at
  most 4 aliquots per fill; a pour is "in band" within ±10 %; a tube is "low" under 1.0 mL; **sample A must go into a
  well before any reagent**; each tube picked up once per plan (merge its pours); the rack must read A, B, C, D left
  to right (the console demos a caught swap).
* Plan moves (`expand`): `grab_A`, `lift(10)`, `travel_to(well1)`, `lower(6)`, `pour(well1, 1.0 mL)`, `lift(6)`,
  `travel_to(slot_A)`, `lower(10)`, `release_A`.
* Requests it understands: "run the assay", "pour 1 ml of A into well 2", "only test reagent C on two wells".
* Approvals: a plan runs only after a **named person** approves (console Run tab, or `approve` in the Assistant /
  voice, logged as "<name> (voice)"). The assistant has four tools: `get_bench_state`, `submit_plan`,
  `request_approval`, `get_run_report`; there is no approve tool.
* "simulated" = the console's built-in fake bench, used whenever the real bench is not publishing.

## Agents (`software/laminar/`, Fetch.ai / ASI:One)
The older agent flow (`lab_agent.py`, `planner_agent.py`) speaks of a **pour station** with one target **tube**
("tube1"): "pour 2 mL of blue into tube1". Vials there are named by id and liquid colour; approve / cancel take a
6-character plan id (`approve 3fa9c1`). It is deterministic (no model) unless `LAMINAR_PLANNER=claude`.

## Arm, cameras and units
| Name | Meaning |
| --- | --- |
| `J1`-`J6` | arm joints, base to wrist, **degrees** from the follower zero (`follower_calibration.json`, zero = folded rest). J1 = base turn, J2-J3 = the two big lifting joints, J4-J5 = wrist bend, **J6 = wrist roll** (the old pour joint). Limits in `02-hardware.md`. |
| `J7` | the gripper (degrees of motor travel; ~38-47 = closed on a vial, 0 = open). |
| leader / follower | leader = StarArm 102 the human moves; follower = the reBot B601-RS that copies it (teleop). |
| rest | all joints ~0 (folded). The arm always ends here, motors off. |
| `vial_X_initial_position` | the grasp pose for vial X (end of its grab path) in `spots.yaml`. |
| `vial_X_final_position` | the pour pose taught by teleop over **container x** (same letter lowercased): vial_A_final -> over a, etc. |
| scan pose | the fixed pose from which the wrist camera photographs the tray markers. |
| carry height / lift | 150 mm above the grasp height (`TRANSFER_LIFT_MM`); `EXTRA_LOWER_MM` = 40 mm deeper on every final lower. |
| tray frame | x = along the tray from a to d, y = across it; offsets in `container_offsets.yaml` / `vial_offsets.yaml` are in this frame, mm. |
| arm base frame | x away from the base, y to the arm's left, z up, mm (`motion.fk_mm`; tip is 166 mm along tool x). |
| wrist camera (`cam=wrist`, index 1) | on the arm; finds the markers; `robotics/wrist_latest.jpg`. |
| side camera / tray cam / level camera (`cam=side`, index 2) | looks at the reservoir from the side; the level model reads it; `robotics/side_latest.jpg` (cropped annotated view). |
| sag | how far the tip hangs below the commanded point under load (20-40 mm far out); corrected from the encoders. |
| wrong column | liquid rising in a channel other than the target: the pour aborts, the episode may shift one channel and retry. |

## Inconsistencies to know about (not yet fixed)
* **Colours of C and D:** `robotics/layout.yaml` says `vial_C: green, vial_D: yellow`; the website (`bench.ts`,
  `lib.ts`, `mana.ts`) says **C = yellow, D = green**. Check the real tubes on the bench before trusting either.
* **Capacity:** the website's well capacity is 3.0 mL; the level model's full column box is 4.5 mL. Pour percentages
  in robotics use the 4.5 mL box.
* `layout.yaml` / `transfer.py` (the older route-based transfer) use `well_1..well_4` and are superseded by the
  marker-based `deliver.py`.
