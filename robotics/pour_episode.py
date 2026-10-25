"""A full pour episode watched by both cameras: python pour_episode.py A2b[:50] [B2b:+50 ...] --run

deliver.py's job (find the tray, grab the vial, carry it to the container's pour pose, back to its slot) with the
level model doing the pour at the pour pose (software/laminar/pour_motor6: tilt the wrist until the liquid comes,
steer by the fill rate, stop at the target, abort on a wrong column), while
    - the SIDE camera (level model, boxes from sense/live_view.py, opened wide) logs every container's level the
      whole episode, and the pour reads the same tracker (one process owns the camera);
    - the WRIST camera streams (wrist_stream.py: markers.py reads its latest frame for the tray scan) and a
      frame is kept twice a second for the review.
Everything lands in logs/episode_<job>_<time>/: levels.csv (t, phase, mL / confidence per column), wrist/*.jpg,
phases.json, pour.json and summary.json (levels at the start, before and after the pour, at the end).

Boxes A-D = containers a-d (draw them left to right in live_view.py). Columns x1, x2, ... between and beside them
are watched for liquid going astray.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:0] = [str(REPO / "sense"), str(REPO / "software")]

import deliver as D  # noqa: E402
import markers as T  # noqa: E402

CAPACITY_ML = 4.5      # mL in a full column (live_view default)
LEVEL_HZ = 10.0
WRIST_HZ = 2.0


def take_side_camera(cam: int) -> list[dict]:
    """Stop any sense/live_view.py that owns side camera `cam` (Windows: one process per camera) and return how to
    restart it. The dashboard keeps its side frames meanwhile: Watch writes side_latest.jpg the same way."""
    import psutil
    stopped = []
    for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
        cmd = p.info.get("cmdline") or []
        if any(c.endswith("live_view.py") for c in cmd) and ("--cam" not in cmd or cmd[cmd.index("--cam") + 1] == str(cam)):
            stopped.append({"cmdline": cmd, "cwd": p.info.get("cwd") or str(REPO / "sense")})
            print(f"  side camera {cam}: stopping live_view.py (pid {p.pid}) for the episode, restarted after it")
            p.terminate()
            try:
                p.wait(5)
            except psutil.TimeoutExpired:
                p.kill()
    if stopped:
        time.sleep(1.0)   # let Windows release the camera
    return stopped


def give_back_side_camera(stopped: list[dict]) -> None:
    if stopped:
        time.sleep(1.0)   # the episode's camera has just been released
    for s in stopped:
        subprocess.Popen(s["cmdline"], cwd=s["cwd"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        print("  live_view.py restarted: " + " ".join(s["cmdline"][1:]))


def wrist_stream_running(max_age_s: float = 1.0) -> bool:
    """A wrist_stream.py is already writing frames (the dashboard's): read those instead of starting another."""
    try:
        return time.time() - T.STREAM_FILE.stat().st_mtime < max_age_s
    except OSError:
        return False


class Watch:
    """The side camera's levels and the wrist camera's frames, logged in the background, tagged by phase."""

    def __init__(self, out: Path, side_cam: int, seg_model: str):
        import live_view
        from cameras import OPERATION_MODE, LatestFrame, open_camera
        self.out, self.phase, self.phases = out, "start", [("start", time.time())]
        self.cam = LatestFrame(open_camera(side_cam, mode=OPERATION_MODE)).start()
        time.sleep(0.5)
        boxes = live_view.get_boxes(self.cam, False)
        if not boxes:
            self.cam.stop()
            raise SystemExit("no level boxes: draw them first (cd sense && python live_view.py --cam N --new-boxes)")
        self.tracker, self.names, self.targets = live_view.build(self.cam, boxes, seg_model, CAPACITY_ML,
                                                                 live_view.cam_width(self.cam))
        (out / "wrist").mkdir(parents=True, exist_ok=True)
        (out / "side").mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._levels, daemon=True),
                         threading.Thread(target=self._wrist, daemon=True),
                         threading.Thread(target=self._side_view, daemon=True)]
        for t in self._threads:
            t.start()

    def set_phase(self, name: str) -> None:
        self.phase = name
        self.phases.append((name, time.time()))
        print(f"[phase] {name}")

    def snapshot(self) -> dict:
        out = {}
        for n in self.names:
            r, raw = self.tracker.read_stable(n), self.tracker.read(n)     # damped for people; raw kept too
            out[n] = {"ml": round(float(r.ml), 3), "raw_ml": round(float(raw.ml), 3), "conf": round(float(raw.conf), 2)}
        return out

    def _levels(self) -> None:
        with open(self.out / "levels.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "phase"] + [f"{n}_{k}" for n in self.names for k in ("ml", "raw_ml", "conf")])
            while not self._stop.is_set():
                snap = self.snapshot()
                w.writerow([f"{time.time():.2f}", self.phase] + [v for n in self.names
                                                                   for v in (snap[n]["ml"], snap[n]["raw_ml"],
                                                                             snap[n]["conf"])])
                f.flush()
                time.sleep(1.0 / LEVEL_HZ)

    def _wrist(self) -> None:
        n, last = 0, 0.0
        while not self._stop.is_set():
            try:
                m = T.STREAM_FILE.stat().st_mtime
                if m != last:
                    last = m
                    shutil.copyfile(T.STREAM_FILE, self.out / "wrist" / f"{n:04d}_{self.phase.replace(' ', '_')}.jpg")
                    n += 1
            except OSError:
                pass
            time.sleep(1.0 / WRIST_HZ)

    def _side_view(self) -> None:
        """Live window of the side camera (this process owns it): each box with its mL, the phase; a frame kept
        twice a second in side/, cut down to the tray (the levels still come from the full frame). All drawing
        happens in this one thread (OpenCV windows want that)."""
        import cv2
        import live_view
        side_latest = live_view.LatestJpeg()
        n, t_save = 0, 0.0
        while not self._stop.is_set():
            frame, _ = self.cam.read()
            if frame is None:
                time.sleep(0.05)
                continue
            img = frame.copy()
            for name in self.names:
                x, y, w, h = (int(v) for v in self.tracker.columns[name].roi)
                r = self.tracker.read_stable(name)
                target = name in self.targets
                col = (0, 200, 0) if target else (150, 150, 150)
                cv2.rectangle(img, (x, y), (x + w, y + h), col, 2 if target else 1)
                if target:
                    cv2.putText(img, f"{name} {r.ml:.2f} mL", (x, max(15, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                col, 2)
            img = live_view.tray_view(img, [c.roi for c in self.tracker.columns.values()], 30, bottom=8)  # the tray
            img = cv2.copyMakeBorder(img, 0, 34, 0, 0, cv2.BORDER_CONSTANT, value=(30, 30, 30))  # strip for the phase
            cv2.putText(img, f"phase: {self.phase}", (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 0, 255), 2)
            if time.time() - t_save > 1.0 / WRIST_HZ:
                t_save = time.time()
                cv2.imwrite(str(self.out / "side" / f"{n:04d}_{self.phase.replace(' ', '_')}.jpg"), img)
                n += 1
            side_latest.offer(img)          # the website shows this same view (publish.py), never blocks
            cv2.imshow("side camera levels", img)
            cv2.waitKey(1)
            time.sleep(0.05)
        cv2.destroyWindow("side camera levels")

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self.tracker.stop()
        self.cam.stop()


DRY_TILT_DEG_S = 15.0     # the pour's own search speed
DRY_HOLD_S = 3.0


def dry_tilt(joint, direction: int, max_tilt: float, watch: "Watch"):
    """Tilt straight to max_tilt (capped at the joint's reach), hold, back to 0: the pour motion without the camera."""
    import math
    from types import SimpleNamespace
    base = joint.position()
    limit = joint.hi if direction > 0 else joint.lo
    reach = min(max_tilt, math.degrees((limit - base) * direction) - 1.0)
    print(f"  dry tilt: to {reach:.0f} deg at {DRY_TILT_DEG_S:g} deg/s, hold {DRY_HOLD_S:g} s, back")
    joint.move_to(base + direction * math.radians(reach), vlim=math.radians(DRY_TILT_DEG_S))
    t0 = time.time()
    while abs(math.degrees(joint.position() - base)) < reach - 0.5 and time.time() - t0 < reach / DRY_TILT_DEG_S + 15:
        time.sleep(0.1)
    watch.set_phase("dry tilt at max")
    print(f"  at {math.degrees(joint.position() - base) * direction:.1f} deg")
    time.sleep(DRY_HOLD_S)
    joint.move_to(base, vlim=math.radians(DRY_TILT_DEG_S * 2))
    while abs(math.degrees(joint.position() - base)) > 0.5:
        time.sleep(0.1)
    return SimpleNamespace(ok=True, result="dry", reason=f"tilted to {reach:.0f} deg and back", max_tilt_deg=reach)


def make_pour(watch: Watch, column: str, target: float, max_tilt: float, record: dict, mode: str = "tip",
              dry: bool = False):
    """deliver.HOOKS["pour"]: the level-model pour into `column`, tipping the vial's top towards the tray.
    mode "tip" (default): every joint tips the gripper about a horizontal axis through the gripper tip (pour_tilt.py),
    the tip locked in place; first the gripper turns in place to the heading from which the tilt goes furthest.
    mode "roll": wrist roll (J6) only, as before (stops at J6's 90 deg limit).
    dry: no vision control: tilt straight to max_tilt (or as far as the arm goes) at DRY_TILT_DEG_S, hold, back
    upright: checks the motion with an empty vial."""
    from laminar import pour_motor6 as P6
    import motion as M
    import markers as T
    import pour_tilt as PT

    def pour(client, pose, cont):
        q = client.current_joints()
        toward = D.into_tray(pose)
        record["before"] = watch.snapshot()
        want = target() if callable(target) else target     # e.g. "+50 %" is measured right now, before the pour
        # om/pour-fixes made the wrong-column alarm 0.2 mL for 0.2 s; on this rig an empty watched column at the edge
        # of the side view reads 0.6-2 mL of noise (people / glare), which aborted real pours. Keep PourGuide's own
        # 0.3 mL for 0.5 s, raised to 0.5 mL: a real stream into the wrong column passes that in well under a second
        # only the 2 channels each side of the target are watched (x1, at the far edge, read noise and aborted)
        cfg = P6.PourConfig(target_frac=want, max_tilt_deg=max_tilt, wrong_column_ml=0.5, wrong_column_s=0.5,
                            watch_near=2)
        turn = None
        if mode == "tip":
            g, q_start, table = PT.best_pour_start(q, toward)
            reach = (len(table) - 1) * PT.TILT_STEP_DEG
            record["tip"] = {"heading_turn_deg": g, "reach_deg": reach}
            print(f"  all-joint pour into {column}: gripper turned {g:+.0f} deg about vertical first, tilt reaches "
                  f"{reach:.0f} deg with the tip locked; target {want:.0%} of the box, max tilt "
                  f"{min(max_tilt, reach):.0f} deg")
            if g:
                turn = M.plan_turn(q, M.fk_mm(q_start)[1], label="turn to the pour heading")
                print("  " + str(client.run(turn).get("result")))
            joint, direction = PT.TipTiltJoint(client, table), 1
        else:
            direction = int(D.tilt_sign(q, toward))
            print(f"  level-model pour into {column} ({'right' if direction > 0 else 'left'}, J6 "
                  f"{'+' if direction > 0 else '-'}), target {want:.0%} of the box, max tilt {max_tilt:g} deg")
            joint = P6.ServerTiltJoint(6)
        try:
            if dry:
                r = dry_tilt(joint, direction, max_tilt, watch)
            else:
                r = P6._pour(joint, watch.tracker, column, direction, empty=[], cfg=cfg,
                             log_path=str(watch.out / "pour_guide.jsonl"))
        finally:
            joint.close()      # upright again: arm_server takes the arm back to where the stream started
        time.sleep(1.0)
        record["after"] = watch.snapshot()
        record["result"] = {k: v for k, v in r.__dict__.items() if k not in ("log", "guide")}
        print(f"  pour: {r.result} - {r.reason}")
        if turn is not None:   # turn back to the pour pose's heading, the tip still in place
            q_now = client.current_joints()
            print("  " + str(client.run(M.plan_turn(q_now, M.fk_mm(q)[1], label="turn back")).get("result")))
        # the return plans from the commanded joints: make sure the arm really is back where the pour began
        q2 = client.current_joints()
        if max(abs(q2[j] - q[j]) for j in range(1, 7)) > 1.0:
            client._send({"op": "move", "joints": {str(j): v for j, v in q.items()}, "speed": 15.0})

    return pour


TARGET_CAP = 0.95   # "100 %" = this fraction of the column box (PourConfig's overflow margin is 0.05); every
                    # requested percent is scaled by it, so 50 % = 47.5 % of the box and 100 % never overflows


def parse_spec(spec: str, default: float) -> tuple[str, str, float]:
    """'A2b' -> ('A2b', 'abs', default); 'A2b:50' -> fill the column to 50 %; 'B2b:+50' -> add 50 % on top of what
    the level camera measures just before that pour. Percent of a FULL column, where full = TARGET_CAP of the box."""
    job, _, pct = spec.partition(":")
    D.parse(job)                                  # validates "A2b"
    if not pct:
        return job, "abs", default
    kind = "add" if pct.startswith("+") else "abs"
    frac = float(pct.lstrip("+").rstrip("%")) / 100.0
    if not 0.0 < frac <= 1.0:
        raise SystemExit(f"{spec!r}: the percent must be between 0 and 100")
    return job, kind, frac


def resolve_target(kind: str, frac: float, watch: "Watch", column: str) -> tuple[float, float]:
    """(target as a fraction of the BOX, level before the pour as a fraction of FULL). frac is a fraction of full
    (full = TARGET_CAP of the box); 'add' is measured on the spot by the level camera."""
    box_h = float(watch.tracker.columns[column].roi[3])
    now_box = watch.tracker.read_stable(column).px / box_h if box_h else 0.0
    now = now_box / TARGET_CAP
    want = frac if kind == "abs" else now + frac
    if want > 1.0:
        print(f"  target {want:.0%} capped at 100 % (= {TARGET_CAP:.0%} of the box)")
    want = min(want, 1.0)
    if want <= now + 0.02:
        print(f"  WARNING: column {column} is already at {now:.0%}, target {want:.0%}: the pour will stop at once")
    return want * TARGET_CAP, now


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jobs", nargs="+",
                    help="vial 2 container, optionally with a fill: A2b (to --target), A2b:50 (to 50 %% of the "
                         "column), B2b:+50 (add 50 %% to what is there); several run one after another")
    ap.add_argument("--run", action="store_true", help="move the arm (arm_server.py must be running)")
    ap.add_argument("--target", type=float, default=0.9, help="default fill, fraction of full (full = 95 %% of the box)")
    ap.add_argument("--max-tilt", type=float, default=140.0, help="never tip the vial further than this (deg)")
    ap.add_argument("--dry-tilt", action="store_true", help="no vision: tilt to --max-tilt and back (empty vial)")
    ap.add_argument("--pour-mode", choices=["tip", "roll"], default="tip",
                    help="tip: every joint tips the gripper, tip locked (default); roll: wrist roll J6 only")
    ap.add_argument("--side-cam", type=int, default=2, help="side (level) camera index")
    ap.add_argument("--wrist-cam", type=int, default=1, help="wrist camera index")
    ap.add_argument("--seg-model", default=str(REPO / "sense" / "level_seg.onnx"))
    a = ap.parse_args()
    if not a.run:
        raise SystemExit("this runs the real arm and both cameras: add --run (plan only: python deliver.py JOB "
                         "--image <scan photo>)")
    specs = [parse_spec(s, a.target) for s in a.jobs]
    columns = ["ABCD"[D.parse(job)[1] - 1] for job, _, _ in specs]
    name = "_".join(j.replace(":", "-").replace("+", "plus") for j in a.jobs)
    out = HERE / "logs" / time.strftime(f"episode_{name}_%Y%m%d_%H%M%S")
    out.mkdir(parents=True)
    print(f"logging to {out.relative_to(HERE)}")

    # share the cameras with the dashboard (publish.py reads wrist_latest.jpg / side_latest.jpg / arm_status.json):
    # reuse a running wrist stream, borrow the side camera from live_view.py and give it back afterwards
    own_stream = not wrist_stream_running()
    stream = subprocess.Popen([sys.executable, str(HERE / "wrist_stream.py"), "--camera", str(a.wrist_cam)]) \
        if own_stream else None
    if not own_stream:
        print("  wrist camera: using the wrist_stream.py already running (dashboard feed)")
    borrowed = take_side_camera(a.side_cam)
    try:
        watch = Watch(out, a.side_cam, a.seg_model)
    except BaseException:
        if stream:
            stream.terminate()
        give_back_side_camera(borrowed)
        raise
    missing = sorted({c for c in columns if c not in watch.targets})
    if missing:
        watch.stop()
        if stream:
            stream.terminate()
        give_back_side_camera(borrowed)
        raise SystemExit(f"no level box {', '.join(missing)}: boxes are {', '.join(watch.targets)}")
    time.sleep(2.0)
    records, err = [], None
    D.HOOKS["phase"] = watch.set_phase
    try:
        for (job, kind, frac), column in zip(specs, columns):
            record: dict = {"job": job, "column": column, "fill": f"{'+' if kind == 'add' else ''}{frac:.0%}",
                            "start": watch.snapshot()}
            records.append(record)
            print(f"=== {job}: {'add ' if kind == 'add' else 'fill to '}{frac:.0%} of column {column}")

            def target_now(kind=kind, frac=frac, column=column, record=record):
                want, before = resolve_target(kind, frac, watch, column)
                record["target"], record["level_before"] = round(want, 3), round(before, 3)
                print(f"  column {column} is at {before:.0%}: pouring to {want / TARGET_CAP:.0%} "
                      f"({want:.0%} of its box)")
                return want

            D.HOOKS["pour"] = make_pour(watch, column, target_now, a.max_tilt, record, a.pour_mode, a.dry_tilt)
            try:
                D.main([job, "--run", "--camera", str(a.wrist_cam)])
            except SystemExit as e:
                if e.code not in (0, None):
                    err = f"{job}: {e}"
            except Exception as e:  # noqa: BLE001 - keep the logs whatever happened
                err = f"{job}: {e!r}"
            time.sleep(1.0)
            record["end"] = watch.snapshot()
            if err:
                print(f"not running the rest: {err}")
                break
    finally:
        watch.stop()
        if stream:
            stream.terminate()
        give_back_side_camera(borrowed)
        (out / "summary.json").write_text(json.dumps({"jobs": records, "error": err, "phases": watch.phases},
                                                     indent=2))
        print(f"episode {'STOPPED: ' + err if err else 'done'}; logs in {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
