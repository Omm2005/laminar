"""The bench computer's link to the dashboard: reports what is connected, shows the side camera + level model,
calibrates on request, and runs approved plans on the real arm.

    python console/bridge/run_plan.py --cam 0               # the real bench (arm_server.py started separately)
    python console/bridge/run_plan.py --sim                 # simulated arm + camera, for trying the dashboard

Every second it reports to the dashboard (convex/station.ts):
    arm          robotics/arm_server.py answering?                     -> "not connected yet" if not
    camera       the side camera giving frames?
    model        sense/level_seg.onnx present (and its test scores)
    calibration  boxes saved (sense/live_boxes.json)? -> "calibration left". Calibration is done ON THE DASHBOARD:
                 the person drags one box per container on the camera picture there; this program saves them
    containers   the mL the model reads in every box, a camera picture with the boxes and levels drawn on, and
                 the same picture clean (what calibration is drawn on)
The dashboard can also switch the side camera (the numbers change when cameras are plugged in).

An approved plan (chat agent -> a person approved it) runs through the Laminar executor: the same checks, steps
and safe abort as everywhere else. Picking and placing are placeholders until the grab is built: the vial must
already be in the gripper at its slot. STOP on the dashboard ends a pour at once (wrist back upright) and stops
the job before its next step. A plan approved while something was not connected is refused, never queued.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAMINAR = Path(os.environ.get("LAMINAR_REPO", HERE.parent.parent))
for sub in ("software", "robotics", "sense"):
    sys.path.insert(0, str(LAMINAR / sub))

from convex import ConvexClient  # noqa: E402

from laminar import telemetry  # noqa: E402
from laminar.executor import Executor  # noqa: E402
from laminar.lab_agent import bench_view, validate_plan  # noqa: E402

ENV_FILE = HERE.parent / ".env.local"
STATUS_FOR = {"pick": "running", "carry": "running", "pour": "pouring", "verify": "running", "place": "running"}


def read_convex_url() -> str:
    for k in ("CONVEX_URL", "VITE_CONVEX_URL"):
        if os.environ.get(k):
            return os.environ[k]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            m = re.match(r"^\s*(CONVEX_URL|VITE_CONVEX_URL)\s*=\s*(.*)$", line)
            if m:
                return m.group(2).strip().strip("\"'")
    raise SystemExit("CONVEX_URL not set and console/.env.local missing: run `bunx convex dev` in console/ first")


class Dashboard:
    """Failure-tolerant Convex calls: a cloud hiccup never stops the robot."""

    def __init__(self, convex: ConvexClient):
        self.c = convex
        self.lock = threading.Lock()
        self.job_id: str | None = None
        self.step_ids: dict[int, str] = {}
        self.current = -1
        self._last_level = 0.0

    def call(self, path: str, args: dict):
        try:
            with self.lock:
                return self.c.mutation(path, args)
        except Exception as e:
            print(f"  [dashboard {path}] skipped: {e!r}")

    def query(self, path: str, args: dict | None = None):
        try:
            with self.lock:
                return self.c.query(path, args or {})
        except Exception as e:
            print(f"  [dashboard {path}] {e!r}")
            return None

    def stop_requested(self) -> bool:
        s = self.query("control:stopRequested")
        return bool(s and s.get("stop"))

    def upload_jpeg(self, data: bytes) -> str | None:
        url = self.call("station:uploadUrl", {})
        if not url:
            return None
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "image/jpeg"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())["storageId"]

    def on_event(self, rec: dict) -> None:
        """telemetry forwarder: executor / skill events -> dashboard rows."""
        if self.job_id is None:
            return
        ev = rec.get("event")
        if ev == "executor.step":
            self.current = int(rec["index"])
            self.step_ids[self.current] = self.call("steps:start", {"jobId": self.job_id,
                                                                    "name": f"{self.current}:{rec['skill']}"})
            self.call("jobs:setStatus", {"jobId": self.job_id, "status": STATUS_FOR.get(rec["skill"], "running"),
                                         "currentStep": f"{self.current}:{rec['skill']}"})
        elif ev == "skill.end" and self.current in self.step_ids:
            args = {"stepId": self.step_ids[self.current], "ok": bool(rec.get("ok"))}
            tel = rec.get("telemetry") or {}
            if tel.get("placeholder"):            # not built yet: say what it would do, the arm did nothing
                line = f"[placeholder] would {tel.get('would') or rec.get('skill')}: not built yet, printed only, nothing moved"
                print("   " + line)
                self.call("events:log", {"kind": "info", "source": "robot", "jobId": self.job_id, "message": line})
                args["reason"] = f"placeholder: would {tel.get('would') or rec.get('skill')} (printed only)"
            elif rec.get("reason"):
                args["reason"] = str(rec["reason"])[:300]
            self.call("steps:end", args)
        elif ev == "pour.sample" and time.time() - self._last_level > 0.2 and rec.get("ml_now") is not None:
            self._last_level = time.time()
            well = str(rec.get("target", "well_0"))
            self.call("robot:pushLevels", {"jobId": self.job_id, "samples": [
                {"t": time.time() * 1000.0, "ml": float(rec["ml_now"]), "channel": int(well.split("_")[-1])}]})


# ---------------------------------------------------------------------------------------------- the real bench

class RealBench:
    """Side camera + level model on the calibrated boxes (kept open for the live view and shared with the pour),
    and the arm through arm_server."""

    simulated = False

    def __init__(self, cam_index: str, dash: Dashboard, res: tuple[int, int] = (1280, 720)):
        import transfer as T
        import live_view
        self.T, self.LV, self.dash, self.cam_index = T, live_view, dash, cam_index
        self.res = res                      # 1280x720 = the camera's full width (640x480 is a crop of the middle)
        self.layout = T.load_layout()
        self.capacity = T.capacity_ml(self.layout)
        self.model_path = Path(live_view.DEFAULT_MODEL)
        self.cam = self.tracker = None
        self.cam_detail = "not connected yet"
        self.names: list[str] = []
        self.targets: list[str] = []
        self.start_ml: dict = {}
        self.view_state: dict = {}
        self.lock = threading.RLock()       # camera / tracker swap (calibration) vs. readers
        self.busy: str | None = None
        self.frame_size: tuple[int, int] | None = None
        self._skills = None
        self._last_cam_try = 0.0

    # ---- camera + calibration
    def ensure_camera(self) -> None:
        if self.cam is not None or time.time() - self._last_cam_try < 5:
            return
        self._last_cam_try = time.time()
        from cameras import LatestFrame, open_camera
        try:
            self.cam = LatestFrame(open_camera(int(self.cam_index) if str(self.cam_index).isdigit() else self.cam_index,
                                               width=self.res[0], height=self.res[1])).start()
            frame, _ = self.cam.read()
            if frame is not None:
                self.frame_size = (frame.shape[1], frame.shape[0])
            self.cam_detail = f"camera {self.cam_index}" + (f" · {self.frame_size[0]}×{self.frame_size[1]}" if self.frame_size else "")
            self.load_calibration()
        except Exception as e:
            self.cam = None
            self.cam_detail = f"not connected yet: camera {self.cam_index} gives no picture ({e})"

    def boxes(self) -> list | None:
        f = self.LV.BOXES_FILE
        return json.loads(f.read_text()) if f.exists() else None

    def boxes_size(self) -> tuple[int, int] | None:
        """The picture size the boxes were drawn at (sense/live_boxes_size.json; older calibrations: 640x480)."""
        if self.boxes() is None:
            return None
        f = self.LV.BOXES_FILE.with_name("live_boxes_size.json")
        try:
            d = json.loads(f.read_text())
            return int(d["w"]), int(d["h"])
        except (OSError, ValueError, KeyError):
            return 640, 480

    def size_mismatch(self) -> str | None:
        drawn = self.boxes_size()
        if drawn and self.frame_size and tuple(drawn) != tuple(self.frame_size):
            return (f"calibration left: the camera picture is now {self.frame_size[0]}×{self.frame_size[1]}, "
                    f"the boxes were drawn at {drawn[0]}×{drawn[1]}: recalibrate")
        return None

    def load_calibration(self) -> None:
        with self.lock:
            if self.tracker is not None:
                self.tracker.stop()
                self.tracker = None
            boxes = self.boxes()
            if self.cam is None or not boxes or not self.model_path.exists() or self.size_mismatch():
                return
            self.tracker, self.names, self.targets = self.LV.build(self.cam, boxes, str(self.model_path), self.capacity,
                                                                   self.LV.cam_width(self.cam))
            time.sleep(0.5)
            self.start_ml = {n: self.tracker.read(n).ml for n in self.names}
            self.view_state = {}

    def set_boxes(self, boxes: list) -> str:
        """The calibration drawn on the dashboard: one box [x, y, w, h] per container, left to right."""
        if self.cam is None:
            raise RuntimeError("the side camera is not connected yet")
        boxes = sorted([[int(round(v)) for v in b] for b in boxes])
        self.LV.BOXES_FILE.write_text(json.dumps(boxes))
        if self.frame_size:
            self.LV.BOXES_FILE.with_name("live_boxes_size.json").write_text(
                json.dumps({"w": self.frame_size[0], "h": self.frame_size[1]}))
        self.load_calibration()
        self.upload_calibration_picture(boxes)        # Claude sets up the bench from this (convex/agent.ts)
        return f"{len(boxes)} container{'s' if len(boxes) != 1 else ''} calibrated"

    def upload_calibration_picture(self, boxes: list) -> None:
        """The camera picture with the boxes drawn and numbered 1..n, left to right."""
        import cv2
        frame, _ = self.cam.read()
        if frame is None:
            return
        img = frame.copy()
        for i, (x, y, w, h) in enumerate(boxes, 1):
            cv2.rectangle(img, (x, y), (x + w, y + h), (255, 0, 255), 2)
            cv2.putText(img, str(i), (x + 2, max(16, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(img, str(i), (x + 2, max(16, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        sid = self.dash.upload_jpeg(buf.tobytes()) if ok else None
        if sid:
            self.dash.call("station:setFrame", {"storageId": sid, "calib": True})

    def set_camera(self, index: str) -> str:
        with self.lock:
            if self.tracker is not None:
                self.tracker.stop()
                self.tracker = None
            if self.cam is not None:
                self.cam.stop()
                self.cam = None
            self.cam_index, self._last_cam_try = str(index), 0.0
        self.ensure_camera()
        if self.cam is None:
            raise RuntimeError(self.cam_detail)
        return f"using camera {index}" + ("" if self.boxes() is None else ": check the boxes still fit, or recalibrate")

    # ---- what the dashboard shows
    def model_state(self) -> dict:
        if not self.model_path.exists():
            return {"loaded": False, "detail": f"{self.model_path.name} missing: git pull or train it"}
        detail = self.model_path.name
        meta = self.model_path.with_suffix(".json")
        try:
            test = json.loads(meta.read_text()).get("test") or {}
            filled = next((v for k, v in test.items() if k.startswith("filled")), {})
            if filled.get("mean_ml") is not None:
                detail += f" · tested: ±{filled['mean_ml']:.2f} mL on filled containers"
        except Exception:
            pass
        return {"loaded": True, "detail": detail}

    def arm_state(self) -> dict:
        from arm_client import ArmClient
        try:
            ArmClient().current_joints()
            return {"connected": True, "detail": "arm_server running"}
        except Exception as e:
            msg = str(e)
            why = ("arm_server is not running" if "not running" in msg else msg.split(". ")[0])
            return {"connected": False, "detail": f"not connected yet: {why}"}

    def calibration_state(self) -> dict:
        if self.boxes() is None:
            return {"done": False, "detail": "calibration left: draw one box per container"}
        if self.size_mismatch():
            return {"done": False, "detail": self.size_mismatch()}
        if self.tracker is None:
            return {"done": False, "detail": "calibrated, waiting for the camera and model"}
        return {"done": True, "detail": f"{len(self.targets)} containers calibrated"}

    def containers(self) -> list[dict]:
        if self.tracker is None:
            return []
        skills = self.skills()
        out = []
        for i, name in enumerate(self.targets, 1):
            s = self.tracker.read(name)
            well = f"well_{i}"
            reachable = well in skills.world.targets and any(not skills.reachable(v, well) for v in skills.world.vials)
            out.append({"number": i, "box": name, "ml": round(float(s.ml), 3), "conf": round(float(s.conf), 2),
                        "capacityMl": self.capacity, "reachable": bool(reachable)})
        return out

    def read_hz(self) -> float | None:
        if self.tracker is None or not self.targets:
            return None
        from laminar import pour_motor6 as P6
        return float(P6.readings_per_s(self.tracker, self.targets[0]))

    def clean_overlay(self, frame):
        """Dashboard picture: each calibrated container's box, numbered, with its liquid shaded and the mL the model
        reads. (live_view's desktop overlay also shows watch columns and alarms; the dashboard has its own.)"""
        import cv2
        reads = []
        shade = frame.copy()
        for i, name in enumerate(self.targets, 1):
            x, y, w, h = self.tracker.columns[name].roi
            s = self.tracker.read(name)
            sure = s.conf >= 0.5
            reads.append((i, x, y, w, h, s, sure))
            if sure and s.px > 1:
                top = int(round(y + h - max(0.0, min(h, s.px))))
                cv2.rectangle(shade, (x, top), (x + w, y + h), (235, 140, 20), -1)
        img = cv2.addWeighted(shade, 0.4, frame, 0.6, 0)
        for i, x, y, w, h, s, sure in reads:
            color = (255, 255, 255) if sure else (170, 170, 170)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 1, cv2.LINE_AA)
            label = f"{i}  {s.ml:.2f} mL" if sure else f"{i}  unsure"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            ly = max(th + 6, y - 6)
            cv2.rectangle(img, (x - 1, ly - th - 5), (x + tw + 7, ly + 4), (20, 20, 20), -1)
            cv2.putText(img, label, (x + 3, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        return img

    def video_frame(self, raw: bool):
        """(picture, camera timestamp) for the live video: clean, or with the boxes and levels drawn on."""
        with self.lock:
            if self.cam is None:
                return None, None
            frame, ts = self.cam.read()
            if frame is None:
                return None, None
            if raw or self.tracker is None:
                return frame, ts
            return self.clean_overlay(frame), ts

    def frames(self) -> tuple[bytes, bytes] | None:
        """(picture with the boxes and levels drawn on, clean picture) as JPEGs at the camera's resolution."""
        import cv2
        with self.lock:
            if self.cam is None:
                return None
            frame, _ = self.cam.read()
            if frame is None:
                return None
            self.frame_size = (frame.shape[1], frame.shape[0])
            if self.tracker is not None:
                img = self.clean_overlay(frame)
            else:
                img = frame.copy()
                cv2.putText(img, "calibration left", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
        q = [cv2.IMWRITE_JPEG_QUALITY, 75]
        ok1, a = cv2.imencode(".jpg", img, q)
        ok2, b = cv2.imencode(".jpg", frame, q)
        return (a.tobytes(), b.tobytes()) if ok1 and ok2 else None

    # ---- running plans
    def skills(self):
        if self._skills is None:
            from laminar.real_skills import RealSkills
            self._skills = RealSkills(
                placeholders=("pick", "place"), stop_requested=self.dash.stop_requested,
                camera_factory=lambda column: self.T.CameraPour.shared(self.cam, self.tracker, column, self.capacity))
        return self._skills

    def system(self):
        from laminar.hardware import System
        from laminar.real_skills import RealArm
        sk = self.skills()
        return System(RealArm(sk.client), None, sk, {})

    def close(self) -> None:
        if self.tracker is not None:
            self.tracker.stop()
        if self.cam is not None:
            self.cam.stop()


class SimBench(RealBench):
    """The same skills on a simulated arm and camera (laminar.real_sim), for trying the dashboard."""

    simulated = True

    def __init__(self, dash: Dashboard, speed: float):
        from laminar.real_sim import make_sim_system
        self.dash, self.busy = dash, None
        self._system = make_sim_system(time_scale=speed, stop_requested=dash.stop_requested)
        self.lock = threading.RLock()
        self.tracker = None

    def ensure_camera(self): pass
    def model_state(self): return {"loaded": True, "detail": "simulated"}
    def arm_state(self): return {"connected": True, "detail": "simulated arm"}
    def calibration_state(self): return {"done": True, "detail": "simulated, 4 containers"}
    def read_hz(self): return None
    def frames(self): return None
    def video_frame(self, raw): return None, None
    def skills(self): return self._system.skills
    def system(self): return self._system
    def boxes(self): return None
    frame_size = None
    cam_index = "sim"
    def close(self): self._system.close()

    @property
    def cam(self):
        return True

    @property
    def cam_detail(self):
        return "simulated camera"

    def containers(self):
        cam = self._system.skills.sim_camera
        return [{"number": i, "box": c, "ml": round(cam.ml[c], 3), "conf": 1.0, "capacityMl": cam.capacity_ml,
                 "reachable": True} for i, c in enumerate("ABCD", 1)]


# ---------------------------------------------------------------------------------------------- live video

STREAM_PORT = 8765
STREAM_FPS = 15


def serve_video(bench: RealBench, port: int = STREAM_PORT):
    """Live MJPEG video for the dashboard (an <img> plays it): /live.mjpg = boxes and levels drawn on,
    /raw.mjpg = the clean picture calibration is drawn on. Straight from this computer, ~15 frames a second."""
    import cv2
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # quiet: one line per frame otherwise
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in ("/live.mjpg", "/raw.mjpg"):
                self.send_error(404)
                return
            raw = path == "/raw.mjpg"
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last = None
            try:
                while True:
                    t0 = time.time()
                    jpg = encoder.get(raw)
                    if jpg is not None and jpg is not last:
                        last = jpg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    time.sleep(max(0.0, 1.0 / STREAM_FPS - (time.time() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                pass                           # the page closed the stream

    class Encoder:
        """Each camera frame is drawn and compressed ONCE, then sent to every open page."""
        def __init__(self):
            self.lock, self.cache = threading.Lock(), {}

        def get(self, raw: bool):
            with self.lock:
                key = "raw" if raw else "live"
                img, ts = bench.video_frame(raw)
                if img is None:
                    return None
                hit = self.cache.get(key)
                if hit and hit[0] == ts:
                    return hit[1]
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    return hit[1] if hit else None
                jpg = buf.tobytes()
                self.cache[key] = (ts, jpg)
                return jpg
    encoder = Encoder()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------------------------- loops

def reporter(bench: RealBench, dash: Dashboard, quit_: threading.Event) -> None:
    """Every second: status + container levels + a camera picture; joint angles twice a second."""
    last_frame = 0.0
    while not quit_.is_set():
        t0 = time.time()
        try:
            arm = bench.arm_state()
            camera = {"connected": bench.cam is not None, "detail": bench.cam_detail}
            dash.call("station:report", {
                "simulated": bench.simulated, "arm": arm, "camera": camera, "model": bench.model_state(),
                "calibration": bench.calibration_state(), "containers": bench.containers(),
                "notReady": bench.skills().not_ready(),
                "grabs": sorted(getattr(bench.skills(), "motions", {}) or {}),
                **({"readHz": bench.read_hz()} if bench.read_hz() is not None else {}),
                **({"busy": bench.busy} if bench.busy else {}),
                **({"frameW": bench.frame_size[0], "frameH": bench.frame_size[1]} if bench.frame_size else {}),
                **({"boxes": bench.boxes()} if bench.boxes() is not None else {}),
                "camIndex": str(bench.cam_index),
                "streamPort": STREAM_PORT,
            })
            if time.time() - last_frame > 3.0:            # fallback picture; the live video is the stream
                last_frame = time.time()
                jpgs = bench.frames()
                if jpgs:
                    for jpg, raw in zip(jpgs, (False, True)):
                        sid = dash.upload_jpeg(jpg)
                        if sid:
                            dash.call("station:setFrame", {"storageId": sid, "raw": raw})
            if arm["connected"]:
                st = bench.skills().client.status() or {}
                src = st.get("state") or st.get("command") or {}
                sample = {"t": time.time() * 1000.0, "joints": [float(src.get(f"J{i}", 0.0)) for i in range(1, 7)]}
                if (st.get("gripper") or {}).get("pos") is not None:
                    sample["gripperDeg"] = float(st["gripper"]["pos"])
                dash.call("robot:pushTelemetry", {**({"jobId": dash.job_id} if dash.job_id else {}),
                                                  "samples": [sample]})
        except Exception as e:
            print(f"  [report] {e!r}")
        quit_.wait(max(0.1, 0.5 - (time.time() - t0)))


def run_one(bench: RealBench, dash: Dashboard, job: dict) -> None:
    dash.job_id, dash.step_ids, dash.current = job["jobId"], {}, -1
    arm = bench.arm_state()
    problem = None if arm["connected"] else f"the robot arm is {arm['detail']}"
    if problem is None and bench.cam is None:
        problem = "the side camera is not connected yet"
    if problem is None and bench.calibration_state()["done"] is False:
        problem = "the side camera is not calibrated yet"
    if problem:                                          # refuse now; never run an old approval later
        print(f"\n== plan {job['jobId']} refused: {problem}")
        dash.call("jobs:finish", {"jobId": dash.job_id, "status": "failed",
                                  "failReason": f"{problem}. Nothing moved"})
        dash.job_id = None
        return
    system = bench.system()
    view = bench_view(system)
    steps = []
    for s in job.get("plan") or []:
        args = dict(s.get("args") or {})
        if s["name"] == "pour":                       # the robot's liquid name for this vial, not the dashboard's
            vial = next((st["args"]["vial_id"] for st in reversed(steps) if st["skill"] == "pick"), None)
            args["liquid"] = (view["vials"].get(vial) or {}).get("liquid")
        steps.append({"skill": s["name"], "args": args})
    plan = {"plan_id": job["jobId"], "request": "dashboard plan", "steps": steps}
    print(f"\n== plan {job['jobId']}: {len(steps)} steps, approved by {job.get('approvedBy')} ==")
    ex = Executor(system, validator=lambda p, b: validate_plan(p, bench_view(system)))
    done = threading.Event()

    def watch_stop():                                 # STOP on the dashboard: no further steps
        while not done.wait(0.3):
            if dash.stop_requested():
                ex.cancel("STOP pressed on the dashboard")
                return
    threading.Thread(target=watch_stop, daemon=True).start()
    dash.call("jobs:setStatus", {"jobId": dash.job_id, "status": "running"})
    bench.busy = "running a plan"
    try:
        report = ex.run(plan, approved_by=job.get("approvedBy") or "dashboard").to_dict()
    finally:
        done.set()
        bench.busy = None
        system.skills.close()                         # ends this job's recording; the shared camera stays on
    status = "done" if report["status"] == "done" else "failed"
    if "STOP" in str((report.get("abort") or {}).get("reason", "")) or \
            any("stopped by operator" in str(s.get("reason", "")) for s in report["steps"]):
        status = "estop"
    why = None
    if report.get("rejected"):
        why = "the plan failed the robot's checks: " + "; ".join(report["rejected"])
    elif report.get("abort"):
        why = report["abort"].get("reason")
    print(f"   -> {report['status']}" + (f": {why}" if why else ""))
    take_from_vials(dash, report)
    dash.call("jobs:finish", {"jobId": dash.job_id, "status": status, "report": report,
                              **({"failReason": str(why)[:300]} if why else {})})
    dash.job_id = None


def take_from_vials(dash: Dashboard, report: dict) -> None:
    """Containers show what the camera reads; vials are not seen by a camera, so subtract what was poured."""
    vial = None
    for s in report["steps"]:
        if s["skill"] == "pick" and s["status"] == "ok":
            vial = s["args"]["vial_id"]
        if s["skill"] == "pour" and s["telemetry"].get("dispensed_ml") is not None and vial:
            ml = float(s["telemetry"]["dispensed_ml"])
            slot = ord(vial.split("_")[-1]) - 64
            row = next((v for v in dash.query("vials:list") or [] if int(v["slot"]) == slot), None)
            if row:
                dash.call("vials:upsert", {"slot": slot, "letter": row["letter"], "contents": row["contents"],
                                           "volumeMl": max(0.0, float(row["volumeMl"]) - ml)})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cam", default="0", help="side camera index")
    ap.add_argument("--res", default="1280x720", help="camera picture size (1280x720 = full width; 640x480 crops)")
    ap.add_argument("--sim", action="store_true", help="simulated arm and camera")
    ap.add_argument("--speed", type=float, default=0.3, help="--sim: time scale (0.3 = 3x faster)")
    a = ap.parse_args()

    try:
        os.nice(10)        # arm_server must never wait for us: it stops the arm after 0.25 s without motor feedback
    except OSError:
        pass
    url = read_convex_url()
    dash = Dashboard(ConvexClient(url))
    telemetry.add_forwarder(dash.on_event)
    res = tuple(int(v) for v in a.res.lower().split("x"))
    bench = SimBench(dash, a.speed) if a.sim else RealBench(a.cam, dash, res)
    quit_ = threading.Event()
    try:
        serve_video(bench)
        print(f"live video: http://localhost:{STREAM_PORT}/live.mjpg")
    except OSError as e:
        print(f"live video off ({e}): the dashboard shows a picture a second instead")
    threading.Thread(target=reporter, args=(bench, dash, quit_), daemon=True).start()
    print(f"bench computer: {'SIMULATED' if a.sim else 'REAL'} bench -> dashboard {url} (Ctrl+C stops)")
    try:
        while True:
            bench.ensure_camera()
            cmd = dash.query("station:nextCommand")
            if cmd:
                dash.call("station:updateCommand", {"id": cmd["_id"], "status": "running"})
                args = cmd.get("args") or {}
                try:
                    if cmd["kind"] == "set_boxes":
                        msg = bench.set_boxes(args["boxes"])
                    elif cmd["kind"] == "set_camera":
                        msg = bench.set_camera(args["index"])
                    else:
                        raise RuntimeError(f"unknown request {cmd['kind']}")
                    print(f"dashboard: {msg}")
                    dash.call("station:updateCommand", {"id": cmd["_id"], "status": "done", "message": msg})
                except Exception as e:
                    dash.call("station:updateCommand", {"id": cmd["_id"], "status": "failed", "message": str(e)})
                continue
            job = dash.query("jobs:nextPlanForRobot")
            if job:
                run_one(bench, dash, job)
                continue
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nbench computer: stopped")
    finally:
        quit_.set()
        bench.close()


if __name__ == "__main__":
    main()
