# Robotics — Avery
arm.py (MotorBridge driver), IK, S-curve, pose-save script, bench.yaml, pick/carry/place, pour controller, abort handling.

Windows follower driver, leader-arm teleop, recording and the scripted arm server (Amogh): see [TELEOP.md](TELEOP.md).
## Shared moves: lift, lower, travel

Every task is: recorded grab → `lift(100)` → `travel_to(well)` → `lower(60)` → pour → `lift(60)` →
`travel_to(slot)` → `lower(100)` → grab played backwards. The three middle moves are computed here,
not recorded, because a recording only works from the exact pose it was recorded at.

| File | What it does |
| --- | --- |
| `motion.py` | Plans straight-line moves with IK: `plan_lift`, `plan_lower`, `plan_travel`, `plan_sequence`. Pure math, never moves the arm. |
| `arm_client.py` | Sends a planned path to `arm_server.py` (`mode="moves"` works today; `mode="path"` needs the op below). |
| `spots.py` / `spots.yaml` | Taught poses: `slot_A..D` (where each grab ends) and `well_1..4` (pour-ready pose over each well). |
| `move.py` | CLI: plan, run, teach spots. |
| `tests/test_motion.py` | Checks straightness, height, orientation, speed caps and refusals. `python -m pytest -q tests` |

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

Lift and lower keep the gripper's orientation exactly. Travel keeps the height exactly and turns only
about vertical, so an upright tube stays upright; a target that would tilt the tube is refused.

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
    arm.run(p, mode="moves")
```

### Heights must agree

Travel never changes height, so `lift(100)` from a slot then `lower(60)` reaches a well only if the well's
taught pose is 40 mm higher than the slot pose. `--lower-next 60` (or `height_offset_mm=60`) prints the
mismatch; fix it by re-teaching the well pose or changing the lower distance.

### Smooth playback: proposed `path` op for arm_server.py

`mode="moves"` sends one joint move per 10 mm; arm_server eases in and out of each, so the arm pulses
slightly. For liquid, add this op to `arm_server.py` and use `mode="path"`:

```python
elif op == "path":   # {"op": "path", "points": [{"1": deg, ..., "6": deg}, ...], "hz": 50}
    pts = [{int(k): float(v) for k, v in p.items()} for p in c["points"]]
    period = 1.0 / float(c.get("hz", 50.0))
    for k, p in enumerate(pts):
        for m, deg in p.items():
            cmd[m] = clamp(m, deg)
        t_next = time.time() + period
        while time.time() < t_next:
            tick()
        if k % 10 == 0:
            write_status(busy=True, progress=round(k / len(pts), 2), doing="path")
    last["result"] = f"path: {len(pts)} points done"
```

### Tuning knobs (top of `motion.py`)

`DEFAULT_LIFT_SPEED` 20 mm/s, `DEFAULT_LOWER_SPEED` 15 mm/s, `DEFAULT_TRAVEL_SPEED` 30 mm/s,
`MAX_JOINT_SPEED_DEG_S` 45, `POS_TOL_MM` 1, `ANG_TOL_DEG` 2. All are first guesses; tune on the arm.
