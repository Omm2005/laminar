"""Single-joint tilt pour with closed-loop level feedback.

Only pour_joint rotates; every other joint is sent its current command and held.
Flow table drives pour_deg choice (target 2..6 s); lead_ml accounts for in-flight liquid;
polling at 5 Hz; drift abort; step-up on no-change; empty-vial detection.

Usage:
  python pour/adaptive_pour.py <target_ml> --channel <N> [--level manual|opencv]
                                 [--dry-run] [--yes]
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (str(HERE), str(HERE.parent / "teleop"), str(HERE / "primitives")):
    if p not in sys.path:
        sys.path.insert(0, p)

import arm_io
from level_source import LevelSource, default_backend
from _common import POUR, POSES, pose_joints, require_near, plan_line

LOG_FILE = HERE / "logs" / "pours.jsonl"

# "float keys" for the flow table (yaml may parse "40" as int)
FLOW = {float(k): float(v) for k, v in POUR["flow_table_ml_per_s"].items()}
POUR_JOINT = int(POUR["pour_joint"])
DIRECTION = int(POUR["direction"])  # +1 or -1
REST_DEG = float(POUR["rest_deg"])
ONSET_DEG = float(POUR["onset_deg"])
DEFAULT_POUR_DEG = float(POUR["pour_deg"])
TILT_SPEED = float(POUR["tilt_speed_deg_s"])
LEAD_ML = float(POUR["lead_ml"])

DRIFT_TOL_DEG = 2.0
POLL_HZ = 5.0
STEP_UP_DEG = 5.0
STEP_UP_MAX = 3
NO_CHANGE_SECS = 4.0
EMPTY_SECS = 2.0


def pick_pour_angle(target_ml):
    """Pick the smallest flow-table angle whose estimated time (target - lead) / rate is in [2, 6]s.
    If none fit, pick whichever is closest to that window."""
    remaining = max(0.0, target_ml - LEAD_ML)
    scored = []
    for angle, rate in sorted(FLOW.items()):
        if rate <= 0:
            continue
        t = remaining / rate
        score = 0 if 2.0 <= t <= 6.0 else min(abs(t - 2.0), abs(t - 6.0))
        scored.append((score, angle, t))
    if not scored:
        return DEFAULT_POUR_DEG, float("nan")
    scored.sort(key=lambda s: (s[0], s[1]))  # best fit, then smallest angle
    _, angle, est_t = scored[0]
    return angle, est_t


def _send_pour_joint_only(hold, angle, speed, wait=True):
    target = {**hold, POUR_JOINT: angle}
    target = arm_io.clamp_joints(target)
    joints_arg = {str(m): round(target[m], 2) for m in range(1, 7)}
    if wait:
        return arm_io.send_cmd("move", joints=joints_arg, speed=speed, timeout=60.0)
    # async: write cmd without blocking so we can poll level during the tilt
    cmd_id = time.time_ns()
    cmd = {"id": cmd_id, "op": "move", "joints": joints_arg, "speed": speed}
    tmp = arm_io.CMD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd))
    import os
    for _ in range(50):
        try:
            os.replace(tmp, arm_io.CMD_FILE)
            return cmd_id
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError("could not stage arm_cmd.json")


def _drift_check(hold):
    """Raise if any non-pour joint's measured state is > DRIFT_TOL_DEG from its held command."""
    st = arm_io.read_status()
    if not st:
        return
    state = st.get("state") or {}
    for m, v in hold.items():
        sv = state.get(f"J{m}")
        if sv is None:
            continue
        if abs(float(sv) - v) > DRIFT_TOL_DEG:
            raise RuntimeError(f"non-pour J{m} drifted {float(sv) - v:+.1f} deg (> {DRIFT_TOL_DEG})")


def _log(record):
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target_ml", type=float)
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--level", default=default_backend(), choices=["auto", "manual", "opencv"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip confirm before real move")
    ap.add_argument("--convex-url", default=None, help="if set, push each 5 Hz level sample")
    ap.add_argument("--job-id", default=None, help="Convex jobs id to tag levels with")
    args = ap.parse_args()

    convex_client = None
    if args.convex_url and args.job_id:
        try:
            from convex import ConvexClient  # type: ignore
            convex_client = ConvexClient(args.convex_url)
        except Exception as e:
            print(f"[adaptive_pour] disabling level streaming ({e!r})")

    pose = f"channel_{args.channel}_pour"
    if pose not in POSES:
        raise SystemExit(f"pose {pose!r} missing in poses.yaml; fill it with jog.py first")

    expected = pose_joints(pose)
    if args.dry_run and not arm_io.status_fresh():
        current = dict(expected)  # offline dry-run: pretend we're at the expected pose
    else:
        current = arm_io.current_joints()
        require_near(expected, current)

    chosen_angle, est_t = pick_pour_angle(args.target_ml)
    hold = {m: current[m] for m in range(1, 7) if m != POUR_JOINT}

    print(f"adaptive_pour: target={args.target_ml:.2f} mL into channel {args.channel}")
    print(f"  pour_joint=J{POUR_JOINT} direction={DIRECTION:+d}")
    print(f"  tilt sequence: rest={REST_DEG:.1f} -> onset={ONSET_DEG:.1f} -> pour={chosen_angle:.1f}  "
          f"(estimated {est_t:.1f} s at {FLOW.get(chosen_angle, float('nan')):.2f} mL/s)")
    print(f"  lead_ml={LEAD_ML:.2f}  tilt_speed={TILT_SPEED} deg/s")
    print(f"  hold (all other joints): {hold}")
    if args.dry_run:
        return
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted"); return

    lv = LevelSource(channel=args.channel, backend=args.level)
    start_ml = lv.prompt("starting mL (vial-into-channel baseline)")

    _send_pour_joint_only(hold, ONSET_DEG, TILT_SPEED)
    _send_pour_joint_only(hold, chosen_angle, TILT_SPEED)

    t_pour_start = time.time()
    t_last_change = t_pour_start
    last_ml = lv.read()
    started_rising = False
    t_started_rising = None
    t_last_rise = None
    step_ups = 0
    cur_pour_deg = chosen_angle
    end_reason = None

    # 3x the estimated time, bounded, as a hard timeout for manual (where level never updates)
    hard_timeout = max(10.0, 3 * (est_t if est_t == est_t else 6.0))

    def _push_level(ml: float):
        if not convex_client:
            return
        try:
            convex_client.mutation("robot:pushLevels", {
                "jobId": args.job_id,
                "samples": [{"t": time.time() * 1000.0, "ml": float(ml), "channel": args.channel}],
            })
        except Exception as e:
            print(f"[adaptive_pour] pushLevels failed, disabling: {e!r}")
            # suppress further pushes this run
            nonlocal_convex_disable[0] = True

    nonlocal_convex_disable = [False]

    try:
        # one sample at t0 so the chart has a baseline
        _push_level(last_ml)
        while True:
            _drift_check(hold)
            cur_ml = lv.read()
            if convex_client and not nonlocal_convex_disable[0]:
                _push_level(cur_ml)
            poured = cur_ml - start_ml
            if poured >= args.target_ml - LEAD_ML:
                end_reason = f"target reached ({poured:.2f} mL >= {args.target_ml:.2f} - {LEAD_ML:.2f})"
                break
            if cur_ml > last_ml + 0.02:
                t_last_change = time.time()
                if not started_rising:
                    started_rising = True
                    t_started_rising = time.time()
                t_last_rise = time.time()
                last_ml = cur_ml
            # vial empty: started rising then stopped
            if started_rising and t_last_rise and time.time() - t_last_rise > EMPTY_SECS:
                end_reason = f"level stopped rising for {EMPTY_SECS}s after it started — vial empty?"
                break
            # no change for NO_CHANGE_SECS -> step up (max STEP_UP_MAX)
            if time.time() - t_last_change > NO_CHANGE_SECS and step_ups < STEP_UP_MAX:
                cur_pour_deg += DIRECTION * STEP_UP_DEG
                print(f"  no change for {NO_CHANGE_SECS}s — stepping pour_joint to {cur_pour_deg:+.1f}")
                _send_pour_joint_only(hold, cur_pour_deg, TILT_SPEED)
                step_ups += 1
                t_last_change = time.time()
            # hard timeout (relevant for manual backend)
            if time.time() - t_pour_start > hard_timeout:
                end_reason = f"hard timeout at {hard_timeout:.0f}s"
                break
            time.sleep(1.0 / POLL_HZ)
    except RuntimeError as e:
        print(f"  ABORT: {e}")
        end_reason = f"abort: {e}"
    finally:
        _send_pour_joint_only(hold, ONSET_DEG, TILT_SPEED)
        _send_pour_joint_only(hold, REST_DEG, TILT_SPEED)

    pour_duration = time.time() - t_pour_start
    final_ml = lv.prompt("final mL (after untilt)")
    poured_real = final_ml - start_ml
    measured_rate = poured_real / pour_duration if pour_duration > 0 else 0.0
    table_rate = FLOW.get(chosen_angle, float("nan"))

    print()
    print(f"result: target={args.target_ml:.2f} mL, poured={poured_real:.2f} mL, "
          f"error={poured_real - args.target_ml:+.2f} mL  ({end_reason})")
    print(f"measured mL/s at {chosen_angle:+.1f} deg: {measured_rate:.3f}   "
          f"(flow_table says: {table_rate:.3f})")
    print(f"-> edit pour/data/pour.yaml by hand to update flow_table_ml_per_s[{chosen_angle:.0f}]")

    _log({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": args.channel,
        "target_ml": args.target_ml,
        "start_ml": start_ml, "final_ml": final_ml, "poured_ml": poured_real,
        "error_ml": poured_real - args.target_ml,
        "pour_joint": POUR_JOINT, "direction": DIRECTION,
        "onset_deg": ONSET_DEG, "chosen_angle_deg": chosen_angle, "cur_pour_deg_end": cur_pour_deg,
        "lead_ml": LEAD_ML, "tilt_speed_deg_s": TILT_SPEED,
        "estimated_time_s": est_t, "pour_duration_s": pour_duration,
        "table_rate_ml_per_s": table_rate, "measured_rate_ml_per_s": measured_rate,
        "step_ups": step_ups, "end_reason": end_reason,
        "level_backend": args.level,
    })


if __name__ == "__main__":
    main()
