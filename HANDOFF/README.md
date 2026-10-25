# Laminar robotics hand-off (2026-10-25, ~04:00)

Written for the next agent picking this up mid-hackathon. Read the files in order; each is self-contained.

| File | What's in it |
| --- | --- |
| [00-glossary-and-notation.md](00-glossary-and-notation.md) | **Every name and letter**: liquids/vials A-D (A = blood sample, B-D = reagents 1-3), containers/wells a-d (= channels 1/4/7/10, markers 10-13), job notation `A2b`, `A2b:50`, `B2b:+50`, the assay, phases, joints, frames, units, and where the repo disagrees |
| [01-project-and-rules.md](01-project-and-rules.md) | What Laminar is, the goal, the team, **the user's rules** (git identity, what may be pushed, credits, how they like to work) |
| [02-hardware.md](02-hardware.md) | The rig: arm, leader, cameras, ports, CAN adapter, power, every hardware quirk found so far |
| [03-code-map.md](03-code-map.md) | Repos, worktrees, branches, and every file that matters with what it does |
| [04-how-to.md](04-how-to.md) | Exact commands: start the arm, teleop, teach, record, run an episode, dry run, stop, recover |
| [05-history.md](05-history.md) | What was done, in order, tonight and before, with run results |
| [06-current-state.md](06-current-state.md) | Where everything stands right now: what works, what doesn't, saved numbers |
| [07-open-problems-and-plan.md](07-open-problems-and-plan.md) | Open problems, root causes found, and the plan that was in progress |

## The one-paragraph version

A reBot B601-RS follower arm (driven by teleop from a StarArm 102 leader, or autonomously) picks a vial (A-D) out of a
rack, carries it to a 4-container / 12-channel reservoir ("the tray") that has ArUco markers 10-13 on it, pours a
requested amount into one container under closed-loop control from a side camera running a level-segmentation model,
and puts the vial back. The tray can be anywhere in a working area; its position comes from the wrist camera seeing the
markers. **Status:** every piece works on its own and real pours have landed in container b, but the aim is not yet
reliable across tray positions (the scan-photo -> arm map is off by ~1 channel = ~10 mm when the tray moves) and the
last teleop teaching of pour poses looks inconsistent. The user wants "A2b and B2b to run perfectly with the tray
anywhere". Next step that was about to run: a C2a **dry run** (no liquid) to check the newly taught pose for container
a, then finish the wrist-camera alignment (`robotics/servo.py`) which needs "look poses" taught.

## Where things are on disk

* `C:\Users\amogh\Downloads\laminar-main` - **the working copy** (git worktree of `main`). All robotics runs and all
  commits happen here. Local-only data lives here too (calibration, recordings, logs).
* `C:\Users\amogh\Downloads\laminar` - the original clone; teammates switch it to other branches (it was on
  `tiger-runs`). Do not run the arm from it, do not touch its uncommitted work.
* `C:\Users\amogh\Downloads\laminar-clone` - a clean clone, kept up to date with `git pull` after pushes.
* GitHub: `Omm2005/laminar` (`main`). Push there only.

## Helper scripts used during the session (`HANDOFF/tools/`)
* `mark_final.py A <teleop log>` - save the teleop's current pose as `vial_A_final_position`.
* `teach_pour.py b <teleop log>` - same plus a wrist-camera reference for servo.py (only works where markers are visible).
* `recover_vial.py vial_C` - bring a held vial from wherever the arm is back into its slot, release, rest (arm_server running).
* `can_check.py` - at rest: hold every motor at 100 Hz for 4 s and report feedback gaps (CAN health check).
