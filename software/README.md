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
| `laminar/hardware.py` | `make_system()` — fake or real (`LAMINAR_FAKE=0`, not wired yet) |
| `laminar/demo.py` | Runs one job on the fakes, no executor |
| `laminar/executor.py` | State machine: preconditions → skill → success check, abort to safe pose, run report |
| `laminar/run.py` | Runs a `plans/*.json` through the executor |

Real implementations (Avery in `robotics/`, Amogh in `sense/`) subclass `Arm`, `LevelTracker`, `Skills`, `HeartbeatSource`.

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
