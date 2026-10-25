# pour/  (LEGACY — fallback only)

> **Not the canonical robot pipeline anymore.** The team's canonical pouring code lives in
> `robotics/` (`arm_server.py` + `arm_client.py` + `motion.py` + `transfer.py`) and the console
> drives it via `console/bridge/run_job.py`. Keep `pour/` as a working fallback for the hard-coded
> pick-carry-pour (useful when `transfer.py` isn't set up for a given vial/channel), but any new
> work should go into `robotics/` or `console/bridge/`.

Hard-coded pick-carry-pour on the reBot B601-RS, driven by `robotics/arm_server.py`
(after the upstream merge). `pour/arm_io.py` and `pour/primitives/_common.py` were repointed
from `teleop/` to `robotics/`; the arm_cmd.json / arm_status.json / kinematics contract is
the same, so all primitives + `run_task.py` keep working.
Nothing in here learns from recordings; every pose and every pour parameter is in a YAML file
that you fill in by hand. The only closed-loop signal is the external level camera.

## Cold start

1. **Power the follower.** 48 V on, candleLight USB plugged in, CAN cable seated.
   Replug the candleLight USB if a previous run wedged it (`detect_arm.py` reports 0/7).
2. **If calibration is stale** (e.g. the gripper doesn't close fully at the saved zero),
   put both arms in the zero pose with grippers *fully squeezed shut*, then:
   ```
   python teleop/calibrate_follower.py --yes
   LEADER_PORT=COM3 python teleop/calibrate_leader.py --yes
   ```
3. **Fill the pose file** with jog. jog starts `arm_server` if needed, enables motors to hold
   the current pose, and lets you nudge joint-by-joint:
   ```
   python pour/jog.py
   ```
   For each pose you need (`home`, `vial_N_above`, `vial_N_grasp`, `channel_M_above`,
   `channel_M_pour`): nudge joints with `1..6` + arrows (shift / `[` `]` for ±5°),
   open/close gripper with `g`, then press `s` and type the pose name to save it into
   `data/poses.yaml` (inline comments are preserved). `q` returns to rest and exits.
4. **Edit `data/pour.yaml` by hand** once you know which joint tilts the vial and in which
   direction. Set `pour_joint`, `direction` (+1 or -1), `rest_deg`, `onset_deg`, `pour_deg`,
   `tilt_speed_deg_s`, `lead_ml`. Leave the flow table at the defaults; it gets refined after
   each pour (see below).
5. **Edit `data/channels.yaml`** with the real capacity of each reservoir trough (fill one from
   the vial's mL graduations, measure, and type the number).
6. **Dry-run a task** to see the exact plan before anything moves for real:
   ```
   python pour/run_task.py "pour 4 ml into channel 3 from vial 1" --dry-run
   ```
   arm_server is auto-started (motors enabled, no movement). Each step prints its planned
   joint targets and FK xyz.
7. **Run for real:**
   ```
   python pour/run_task.py "pour 4 ml into channel 3 from vial 1"
   # or:
   python pour/run_task.py "pour half of channel 3 from vial 1" --confirm-each
   ```
   Create a file named `STOP` in `teleop/` to abort before the next primitive.
8. **After each pour**, `adaptive_pour.py` prints the measured mL/s at the chosen angle next to
   the current flow-table value. Edit `pour.yaml`'s `flow_table_ml_per_s` by hand to update it.
   No automatic fitting. All pours are logged line-by-line to `pour/logs/pours.jsonl`.

## Driving pours from the web console

Instead of typing `run_task.py` strings, you can submit jobs in the Laminar Console
(`console/`) and let `robot_client.py` pick them up.

```bash
# in another terminal, console must be running first (see console/README.md)
python pour/robot_client.py --level auto
# add --dry-run to run every primitive in dry-run mode (no motion)
# add --once to pick up a single approved job and exit
```

`robot_client.py` polls `jobs.nextForRobot`, runs each primitive via subprocess (same
as `run_task.py`), pushes `arm_status.json` to Convex at 10 Hz as telemetry, records
the final pour from `logs/pours.jsonl`, and aborts if `control.stopRequested` turns on.

## What's in each file

| file | purpose |
|---|---|
| `data/poses.yaml` | hand-filled joint angles for `home`, each `vial_N_above`/`vial_N_grasp`, each `channel_M_above`/`channel_M_pour`. |
| `data/pour.yaml` | pour mechanics: pour_joint, tilt angles, tilt speed, lead_ml, flow table. |
| `data/channels.yaml` | 12-channel reservoir capacities. |
| `jog.py` | keyboard jog + pose save (writes into `poses.yaml`, preserves comments). |
| `arm_io.py` | shared helpers: start/stop arm_server, send commands, read status, clamp to LIMITS. |
| `level_source.py` | manual backend (prompt mL at pour boundaries) + opencv stub for Amogh's model. |
| `primitives/grasp.py N` | precheck near `vial_N_grasp`, open jaws, grasp. |
| `primitives/lift.py --mm` | 4-waypoint straight-up FK offset, orientation held. |
| `primitives/lower.py --mm` | 4-waypoint straight-down FK offset. |
| `primitives/traverse.py N` | joint-space move to `channel_N_above` with a no-dip check. |
| `primitives/untilt.py` | tilt `pour_joint` back to `rest_deg`, all other joints held. |
| `primitives/return_vial.py N` | joint-space move to `vial_N_above` with no-dip check, then release. |
| `primitives/home.py` | move to the `home` pose. |
| `adaptive_pour.py` | single-joint tilt pour, 5 Hz level polling, drift abort, pour log. |
| `run_task.py` | parse NL, print plan, run primitives, honor `STOP`, `--confirm-each`. |
| `robot_client.py` | polls Convex `jobs.nextForRobot`, drives primitives, streams telemetry, records pours. |

## Rules this code follows

- All movement is through `arm_server.py` (`arm_cmd.json` / `arm_status.json`). No direct CAN.
- Every primitive has `--dry-run` that prints the planned joint targets + FK tool xyz.
- Every primitive clamps targets to `LIMITS` (shared with `teleop/arm_server.LIMITS`).
- Primitives whose start pose is known (grasp, adaptive_pour, return_vial) refuse to move if the
  current pose differs from the expected start by more than 10° on any joint.
- `adaptive_pour.py` only moves `pour_joint`; all other joints are commanded to their current
  values. It aborts to untilt if any non-pour joint drifts by more than 2°.
- `STOP` file in `teleop/` aborts between primitives.

## Known rough edges

- The `teleop/robstride_can.py` `ping()` decode has a bit-field bug (bits 0-7 vs 8-15) that
  makes `detect_arm.py` report 0/7 even when motors reply. The feedback path works. Pour code
  doesn't touch ping.
- `teleop/teleop.py`'s `stop_requested()` + `msvcrt.kbhit()` is unreliable when teleop is launched
  as a background subprocess; the `STOP` file is sometimes not noticed and the process has to be
  killed. Pour code uses `arm_server`, which does see `STOP` reliably.
- The candleLight USB-CAN adapter wedges if a flurry of frames gets no reply (e.g. probing with
  motors off). Replug it when `detect_arm.py` goes silent.
