"""A full pour episode watched by both cameras: python pour_episode.py A2b --run [--target 0.9]

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
            r = self.tracker.read(n)
            out[n] = {"ml": round(float(r.ml), 3), "conf": round(float(r.conf), 2)}
        return out

    def _levels(self) -> None:
        with open(self.out / "levels.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "phase"] + [f"{n}_{k}" for n in self.names for k in ("ml", "conf")])
            while not self._stop.is_set():
                snap = self.snapshot()
                w.writerow([f"{time.time():.2f}", self.phase] + [v for n in self.names
                                                                   for v in (snap[n]["ml"], snap[n]["conf"])])
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
        twice a second in side/, cut off above the tray (the levels still come from the full frame). All drawing
        happens in this one thread (OpenCV windows want that)."""
        import cv2
        import live_view
        n, t_save = 0, 0.0
        while not self._stop.is_set():
            frame, _ = self.cam.read()
            if frame is None:
                time.sleep(0.05)
                continue
            img = frame.copy()
            for name in self.names:
                x, y, w, h = (int(v) for v in self.tracker.columns[name].roi)
                r = self.tracker.read(name)
                target = name in self.targets
                col = (0, 200, 0) if target else (150, 150, 150)
                cv2.rectangle(img, (x, y), (x + w, y + h), col, 2 if target else 1)
                if target:
                    cv2.putText(img, f"{name} {r.ml:.2f} mL", (x, max(15, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                col, 2)
            img = img[live_view.tray_top([c.roi for c in self.tracker.columns.values()], 30):]   # only the tray
            cv2.putText(img, f"phase: {self.phase}", (10, img.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 0, 255), 2)
            if time.time() - t_save > 1.0 / WRIST_HZ:
                t_save = time.time()
                cv2.imwrite(str(self.out / "side" / f"{n:04d}_{self.phase.replace(' ', '_')}.jpg"), img)
                n += 1
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
        cfg = P6.PourConfig(target_frac=target, max_tilt_deg=max_tilt)
        turn = None
        if mode == "tip":
            g, q_start, table = PT.best_pour_start(q, toward)
            reach = (len(table) - 1) * PT.TILT_STEP_DEG
            record["tip"] = {"heading_turn_deg": g, "reach_deg": reach}
            print(f"  all-joint pour into {column}: gripper turned {g:+.0f} deg about vertical first, tilt reaches "
                  f"{reach:.0f} deg with the tip locked; target {target:.0%} of the box, max tilt "
                  f"{min(max_tilt, reach):.0f} deg")
            if g:
                turn = M.plan_turn(q, M.fk_mm(q_start)[1], label="turn to the pour heading")
                print("  " + str(client.run(turn).get("result")))
            joint, direction = PT.TipTiltJoint(client, table), 1
        else:
            direction = int(D.tilt_sign(q, toward))
            print(f"  level-model pour into {column} ({'right' if direction > 0 else 'left'}, J6 "
                  f"{'+' if direction > 0 else '-'}), target {target:.0%} of the box, max tilt {max_tilt:g} deg")
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", help="vial 2 container, e.g. A2b")
    ap.add_argument("--run", action="store_true", help="move the arm (arm_server.py must be running)")
    ap.add_argument("--target", type=float, default=0.9, help="stop at this fraction of the column box")
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
    _, cont = D.parse(a.job)
    column = "ABCD"[cont - 1]
    out = HERE / "logs" / time.strftime(f"episode_{a.job}_%Y%m%d_%H%M%S")
    out.mkdir(parents=True)
    print(f"logging to {out.relative_to(HERE)}")

    stream = subprocess.Popen([sys.executable, str(HERE / "wrist_stream.py"), "--camera", str(a.wrist_cam)])
    watch = Watch(out, a.side_cam, a.seg_model)
    if column not in watch.targets:
        watch.stop()
        stream.terminate()
        raise SystemExit(f"no level box {column} for container {a.job[-1]}: boxes are {', '.join(watch.targets)}")
    time.sleep(2.0)
    record: dict = {"job": a.job, "column": column, "target": a.target, "start": watch.snapshot()}
    D.HOOKS["phase"] = watch.set_phase
    D.HOOKS["pour"] = make_pour(watch, column, a.target, a.max_tilt, record, a.pour_mode, a.dry_tilt)
    err = None
    try:
        D.main([a.job, "--run", "--camera", str(a.wrist_cam)])
    except SystemExit as e:
        err = str(e) if e.code not in (0, None) else None
    except Exception as e:  # noqa: BLE001 - keep the logs whatever happened
        err = repr(e)
    finally:
        time.sleep(1.0)
        record["end"] = watch.snapshot()
        record["error"] = err
        record["phases"] = watch.phases
        watch.stop()
        stream.terminate()
        (out / "summary.json").write_text(json.dumps(record, indent=2))
        print(f"episode {'STOPPED: ' + err if err else 'done'}; logs in {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
