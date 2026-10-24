# Teleop — Amogh

reBot Arm B601-RS follower control, leader-arm teleop and episode recording on Windows.

Teleoperate a Seeed **reBot Arm B601-RS** follower (RobStride CAN motors) with a **reBot Arm 102 / StarArm 102**
leader (FashionStar UART servos) from Windows, and record episodes (camera video + joint data).
Seeed's SDK only supports PEAK adapters on Windows, so this talks to the arm directly through a
**candleLight (gs_usb) USB-CAN adapter** with python-can.

## Hardware

| Part | Connection |
|---|---|
| Follower: reBot Arm B601-RS (J1–J3 RS06, J4–J6 + gripper RS00, CAN IDs 1–7, host 0xFD) | candleLight USB-CAN, 1 Mbps, 48 V supply |
| Leader: reBot Arm 102 (FashionStar servos, IDs 0–6) | CH340 USB-UART (`COM14`), 1 Mbps, 12 V |
| Camera: Innomaker U20CAM 1080p | USB, DirectShow index 2 |

## Setup

```
pip install -r requirements.txt
```

The candleLight adapter enumerates with WinUSB on Windows; `libusb-package` provides the libusb DLL that python-can's gs_usb backend needs.

## Scripts

| Script | What it does |
|---|---|
| `detect_arm.py` | Passive scan: pings follower motors 1–7, prints position and bus voltage. Never enables. |
| `move_arm.py` | Demo: base +30°, then every joint 30° more, hold, return to start. `--dry-run` to preview. |
| `calibrate_leader.py` | Sets each leader servo's origin at the zero pose (stored in the servos). |
| `calibrate_follower.py` | Records the follower's zero pose to `follower_calibration.json` (passive, nothing written to motors). |
| `teleop.py` | Leader → follower teleop. Eases onto the leader pose, then tracks live. `--record` saves video + data. |
| `robstride_can.py` | Minimal RobStride CAN driver (MIT mode, parameter read/write, feedback decode) over gs_usb. |
| `leader_arm.py` | Leader reader + leader→follower joint mapping (directions, gripper ×6, limits). |
| `recorder.py` | Camera grabber thread + fixed-rate recorder (video.mp4, data.csv, meta.json). |
| `rest_arm.py` | Eases the follower from wherever it is (enabled or not) to its zero pose, then disables. |
| `arm_server.py` | Holds the follower and runs one command at a time from `arm_cmd.json` (move / gripper / grasp-until-contact / rest), writing `arm_status.json`. For scripted or agent control. |
| `arm_cmd.py` | CLI for `arm_server.py`: `python arm_cmd.py move 1=-20 2=65`, `grasp`, `gripper 78`, `rest`, `status`. |
| `kinematics.py` | Forward kinematics from the vendor URDF (`model/rebot_b601_rs.urdf`) in the follower's joint frame. |
| `snap.py` | Grabs one frame from the wrist camera. |

### Calibrate (both arms in the zero pose, grippers closed)

```
python calibrate_leader.py
python calibrate_follower.py
```

Calibration files (`leader_calibration.json`, `follower_calibration.json`) are device-specific and git-ignored;
run both scripts on a new setup. Leader zero-pose reference: https://files.seeedstudio.com/wiki/robotics/projects/lerobot/102_zeroposition.jpg

### Teleop + recording

```
python teleop.py --record --fps 20
```

Stop with `q` / `Esc` / `Ctrl+C`, or by creating a file named `STOP` next to the script. The follower returns
slowly to its zero pose and the motors are disabled.

Teleop refuses to start if any joint differs between leader and follower by more than `--max-start-mismatch`
(default 25°). Safety behaviour: per-joint soft limits, speed cap (`--max-speed`, default 180°/s), slow ease-in onto the leader
pose, torque-limited gripper, and automatic stop on lost leader readings, lost follower feedback, motor faults
or motor temperature > 80 °C.

## Recordings

`recordings/<timestamp>/` (git-ignored, not published):

- `video.mp4` — camera at 640×480, 20 fps
- `data.csv` — one row per video frame: `action.J1..J7` (leader target), `command.J1..J7` (rate-limited follower
  command), `state.J1..J7` (measured follower position), all in degrees from the follower zero pose
- `meta.json` — fps, resolution, joint names, follower zero offsets, trim info

## Notes / gotchas found along the way

- **Stale CAN TX queue:** if no motor ACKs (arm unpowered), the candleLight keeps retrying old frames; starting it
  in loopback mode flushes them.
- **gs_usb `recv()` is slow on Windows** (~14 ms when idle), so receiving runs on a background thread.
- **Adapter wedge:** at ~200 Hz × 7 motors the adapter once returned USB I/O errors and stopped transmitting;
  teleop now runs at 100 Hz and always attempts a safe shutdown. Replug the adapter if it wedges.
- **Single-turn wrap:** RobStride reports a single-turn angle after power-up (J6 read +178.6° one day, −179.8° the
  next); teleop unwraps the saved zero to the nearest turn.
- **FashionStar exact-zero filter:** the servo library flags an exact 0.0° reading as unreliable and returns the
  previous value, so right after calibration readings can look stale.
- **Camera:** DirectShow wouldn't switch the U20CAM to MJPG, so 720p only gives 10 fps; 640×480 gives 30 fps.

## References

- Seeed wiki: [B601-RS MIT control](https://wiki.seeedstudio.com/rebot_arm_b601_rs_mit_control/), [B601-RS LeRobot](https://wiki.seeedstudio.com/rebot_arm_b601_rs_lerobot/)
- LeRobot [reBot B601 docs](https://huggingface.co/docs/lerobot/rebot_b601) (leader/follower joint mapping)
- [Seeed-Projects/lerobot-teleoperator-rebot-arm-102](https://github.com/Seeed-Projects/lerobot-teleoperator-rebot-arm-102)
