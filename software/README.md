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
| `laminar/demo.py` | Runs one job on the fakes |

Real implementations (Avery in `robotics/`, Amogh in `sense/`) subclass `Arm`, `LevelTracker`, `Skills`, `HeartbeatSource`.

## Run it

```bash
cd ~/laminar && source .venv/bin/activate && cd software
pytest -q                                         # 35 tests
python -m laminar.demo                            # clean 2 mL blue job
python -m laminar.demo --fault wrong_tube         # staged failure
python -m laminar.demo --vial V2 --liquid red --ml 1.5 --tol 0.4
python -m laminar.demo --speed 1                  # real-time (~20 s)
```

Faults: `wrong_tube`, `grasp_slip`, `drop_in_carry`, `pour_overshoot`, `level_stuck`.
