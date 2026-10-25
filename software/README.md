# Software — Om

Interfaces + fakes, executor state machine, validator, Claude agent + 4 tools, telemetry, voice, dashboard, integrations.

## Layout

| File | What it is |
| --- | --- |
| `laminar/arm.py` | `Arm` contract + `FakeArm`. 6 joints, rad, limits from the URDF |
| `laminar/level.py` | `LevelSample`, `LevelTracker` contract + `FakeLevelTracker` (simulated tube) |
| `laminar/skills.py` | `Skills` contract (`locate/pick/carry/pour/verify/place`) + `FakeSkills` with staged faults |
| `laminar/safety.py` | `Watchdog` (ESTOP or 250 ms silence → `arm.stop()`) + `FakeHeartbeat` |
| `laminar/telemetry.py` | `log(event, data)` → `logs/telemetry.jsonl` |
| `laminar/bench.py` | Loads `robotics/bench.yaml` |
| `laminar/hardware.py` | `make_system()` — fake, or the real arm with `LAMINAR_FAKE=0` (`LAMINAR_CAM` = side camera) |
| `laminar/real_skills.py` | `RealSkills`: pick / carry / pour / verify / place on the real arm (arm_server, spots.yaml, layout.yaml, camera pour with the level model) |
| `laminar/chat.py` | Type to the lab agent in the terminal, simulated or real (`--real`), rules or Claude (`--claude`) |
| `laminar/demo.py` | Runs one job on the fakes, no executor |
| `laminar/executor.py` | State machine: preconditions → skill → success check, abort to safe pose, run report |
| `laminar/run.py` | Runs a `plans/*.json` through the executor |

Real implementations (Avery in `robotics/`, Amogh in `sense/`) subclass `Arm`, `LevelTracker`, `Skills`, `HeartbeatSource`.

## Real arm: from a sentence to a pour

```
"pour 1 ml of blue into well 2"  ->  Intent (rules, or Claude with --claude)  ->  plan built by code
  ->  validate_plan (vial, slot taught, route measured, volume, capacity)  ->  you approve
  ->  Executor: pick -> carry -> pour (camera) -> verify -> carry back -> place   (any failure: wrist upright, hold)
```

Bring-up, once (on the bench):

1. `cd robotics && python mac_run.py arm_server.py` (Windows/Linux: `python arm_server.py`), leave it running.
2. Teach where each vial is grabbed: record a grab with teleop, then
   `python move.py from-recording slot_A recordings/<run>/data.csv` (slot_B, ...).
3. Measure `routes:` in `robotics/layout.yaml`: mm from each vial's slot to above each compartment.
   Check `pour.direction`, `gripper_open_deg`, `approach_mm`, `vials:` (which liquid each holds).
4. Side camera: `cd sense && python live_view.py --cam 0 --new-boxes`, one box per compartment, A-D left to right.
5. `python -m laminar.chat --real --check` lists anything still missing.

Then: `python -m laminar.chat --real --cam 0` (add `--claude` with `ANTHROPIC_API_KEY` set for free-form requests).
Without hardware, `pytest tests/test_real_skills.py` runs whole real-arm jobs on a stand-in arm_server and camera.

## Run it

```bash
cd ~/laminar && source .venv/bin/activate && cd software
pytest -q                                         # 47 tests
python -m laminar.run plans/aliquot_2ml_blue.json --approve Om                     # DONE
python -m laminar.run plans/aliquot_2ml_blue.json --approve Om --fault wrong_tube  # ABORTED at verify
python -m laminar.run plans/aliquot_2ml_blue.json                                  # REJECTED, not approved
python -m laminar.demo                            # clean 2 mL blue job
python -m laminar.demo --fault wrong_tube         # staged failure
python -m laminar.demo --vial V2 --liquid red --ml 1.5 --tol 0.4
python -m laminar.demo --speed 1                  # real-time (~20 s)
```

Faults: `wrong_tube`, `grasp_slip`, `drop_in_carry`, `pour_overshoot`, `level_stuck`, `wrong_color`, `surge`, `high_onset`, `camera_blocked`.

## Camera → motor: PourFrame JSON

The OpenCV side-cam code sends **one JSON object per frame (30 Hz)** as a UDP datagram to `127.0.0.1:5555`.

```json
{"ts": 1761270000.123, "seq": 1042, "ml": 1.42, "flow_ml_s": 0.85, "pouring": true,
 "color": "blue", "color_conf": 0.93, "conf": 0.97, "level_px": 236}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ts` | float | yes | `time.time()` when the frame was captured. Frames older than 150 ms are ignored |
| `ml` | float | yes | Liquid in the tube since the pour started (zeroed), mL |
| `flow_ml_s` | float | | Smoothed rate of rise, mL/s (Savitzky-Golay or EMA on `ml`) |
| `pouring` | bool | | Liquid visibly entering the tube right now |
| `color` | str | | `"blue"`, `"red"`, `"none"` (nothing in the tube), `"unknown"` |
| `color_conf` | float | | 0–1. A wrong colour only counts at ≥ 0.7, for 3 frames in a row |
| `conf` | float | | 0–1 overall. Send < 0.5 if the crop is dark, blocked, or the tube is missing |
| `seq` | int | | Frame counter |
| `level_px` | int | | Raw column height, for debugging |

Sending from Python:

```python
import socket, json, time
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(json.dumps({"ts": time.time(), "ml": ml, "flow_ml_s": flow, "pouring": pouring,
                        "color": color, "color_conf": color_conf, "conf": conf}).encode(),
            ("127.0.0.1", 5555))
```

How the motor reacts (`laminar/pour.py`, `PourController`):

| Camera says | Arm does |
| --- | --- |
| flow above setpoint, or within 0.6 mL of target | `slow_down`: tilts back a little |
| flow > 2.5× setpoint (surge) | `slow_down` hard |
| `ml` + flow × lag ≥ target | `stop`: tilts back, pour done |
| not pouring while tilted | `tilt_more`; at max tilt and still dry 1.5 s → `abort` |
| wrong `color` for 3 frames | `abort`: tilts back |
| stale frames or `conf` < 0.5 | `hold`; for 0.3 s → `abort` |

Try it without hardware (two terminals):

```bash
python -m laminar.pour_live --target 2 --color blue
python -m laminar.fake_camera --scenario normal      # or wrong_color | dry | surge | blocked
```

## Motor test (laptop connected to the arm)

```bash
pip install motorbridge
python -m laminar.motor_test                  # connect + read motor 6, nothing moves
python -m laminar.motor_test --move           # slow ±0.2 rad wiggle and back: expect PASS
python -m laminar.motor_console               # interactive: edges, speeds, sweep, go, home (--fake to try without the arm)
python -m laminar.pour_live --target 2 --color blue --motor real --max-tilt 0.8   # camera drives motor 6
```

Mac + PCAN: `--channel can0@1000000` (default). Ubuntu: `--channel can0`. Confirm joint 6's RobStride model and pass `--model`.
`motor6.py` is untested on hardware — check it against motorbridge's `robstride_wrapper_demo.py`.

## Fetch.ai agent (ASI:One) and Tiger Data telemetry

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

| File | What it is |
| --- | --- |
| `laminar/lab_agent.py` | Chat logic: bench view, request -> plan (built by code), `validate_plan`, approvals, run + report. No network |
| `laminar/fetch_agent.py` | uAgent `laminar-lab`: Agent Chat Protocol, Agentverse mailbox, so it is usable from ASI:One |
| `laminar/tiger.py` | Tiger Data: hypertables, continuous aggregates, compression, background writer, arm tracking sampler |
| `AGENT.md` | The agent's Agentverse profile |
| `laminar/agent_protocol.py` | LaminarPlanner protocol: `BenchQuery/BenchView`, `PlanRequest/PlanProposal`, `RunRequest/RunReport` |
| `laminar/planner_agent.py` | Reference planner + orchestrator agent behind the entry agent (Om's Claude planner replaces `plan_for`) |

The whole workflow runs in an ASI:One chat: `what's on the bench?` -> `pour 2 mL of blue into tube1` (plan,
checked against the bench) -> `approve <id>` -> step-by-step report with the measured volume. Anyone can
ask for a plan; on real hardware only addresses in `LAMINAR_APPROVERS` can approve one. Requests are read
by a rule parser (no API calls); `LAMINAR_PLANNER=claude` reads free-form requests with Claude instead,
and the same code-side validation applies either way.

```bash
pip install -r requirements-agents.txt
export LAMINAR_AGENT_SEED="<long secret phrase>"     # fixed agent address
export LAMINAR_APPROVERS="<your ASI:One address>"
python -m laminar.fetch_agent                         # prints the address + inspector link: Connect -> Mailbox
```

Two agents: `laminar-lab` is the public entry agent (Agentverse listing, Chat Protocol, approvals and the
code-side plan checks). It forwards bench queries, plan requests and approved runs to the planner +
orchestrator agent over the LaminarPlanner protocol, and falls back to its built-in planner if that agent
does not answer. Set `LAMINAR_PLANNER_AGENT=<planner address>` for the entry agent, and run
`python -m laminar.planner_agent` (needs `LAMINAR_PLANNER_SEED`).

| Agent | Role | Address |
| --- | --- | --- |
| `laminar-lab` | Entry agent on Agentverse / ASI:One | `agent1qduarv4s64wtl069d26ydak0q8k8em94rvag344czm4xp9pzr3ht56dp2s8` |
| `laminar-planner` | Planner + orchestrator (reference) | `agent1qvensewvksgpfdvqd7ymj59u7hjzq0g0q739uw2w026h4wxx49d2wff4ysl` |

Tiger Data (TimescaleDB): every `telemetry.log()` event, every pour sample and the arm's per-joint
tracking error, as hypertables with real-time continuous aggregates (`pour_1s`, `arm_tracking_1s`), a
`runs` view and compression after a day. Writes happen on a background thread and never block the bench.

```bash
export TIGER_DATABASE_URL="postgres://tsdbadmin:...@....tsdb.cloud.timescale.com:5432/tsdb?sslmode=require"
python -m laminar.tiger init          # tables, aggregates, policies (safe to re-run)
python -m laminar.tiger backfill      # logs/telemetry.jsonl
python -m laminar.tiger arm           # sample robotics/arm_status.json while arm_server runs
python -m laminar.tiger report        # recent runs, pours, per-joint tracking from the aggregates
python -m laminar.tiger run --name "vial A" -- python ../robotics/transfer.py vial_A well_3   # log a run
python -m laminar.tiger smooth        # per-joint tracking error, jerk, stick-slip for the last run
python -m laminar.tiger import ../robotics/recordings/<episode>   # a teleop recording as a run
```

The fetch agent streams to Tiger automatically when `TIGER_DATABASE_URL` is set.

Runs: `robotics/arm_server.py` writes a 100 Hz trace (commanded / measured / velocity / torque per joint,
`robotics/arm_trace.py`) and `tiger run` uploads the part inside each run to the `arm_trace` hypertable,
with one `arm_runs` row per run. `run_smoothness()` and `run_trace()` are SQL functions in Tiger, so the
CLI and the console's Runs page (branch `tiger-runs`) compute the same numbers.
