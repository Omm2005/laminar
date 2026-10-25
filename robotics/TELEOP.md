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
| Camera: Innomaker U20CAM 1080p (wrist) | USB, DirectShow index 1 or 2: indices shift after re-plugs, snapshot each index to check |

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
| `arm_server.py` | Holds the follower and runs one command at a time from `arm_cmd.json` (move / gripper / grasp-until-contact / path / rest), writing `arm_status.json`. Defaults: every path smoothed (`--raw-paths` off), gravity + friction feedforward from `compensation.json` (`--no-comp` off), gripper holds like teleop (1.0 N·m once closed on an object). Failsafe: lost feedback / faults never drop the arm (see gotchas). `--hold-grip` keeps squeezing an object held at startup. |
| `arm_cmd.py` | CLI for `arm_server.py`: `python arm_cmd.py move 1=-20 2=65`, `grasp`, `gripper 78`, `rest`, `status`. |
| `kinematics.py` | Forward kinematics from the vendor URDF (`model/rebot_b601_rs.urdf`) in the follower's joint frame. |
| `snap.py` | Grabs one frame from the wrist camera. |
| `move_tool.py` | Straight-line tool moves via IK through `arm_server.py`: `--fwd/--left/--up` metres, orientation held. |
| `markers.py` | Wrist-camera tray finder: `view` (live ids + tray outline), `calibrate scan-pose / touch POS ID / photo POS / finish` (step by step), `visit [--run]` (find the tray, lock in, hover 50 mm above each marker centre with sag correction). |
| `deliver.py` | `python deliver.py A2c --run`: find container c, grab vial A (teleop replay), rise 150 / travel / lower 110 over the container (sag-corrected), then everything in reverse. `CONTAINER_OFFSET_MM` = where to go relative to each marker (zero for now). |
| `wrist_stream.py` | Live wrist-camera window + `wrist_latest.jpg` for other scripts (Windows lets one process own a camera). |
| `compensation.py` | Fits gravity + friction feedforward from a Tiger run (`fit --run ID`) into `compensation.json` (committed, this arm). |
| `smooth_path.py` | Zero-phase smoothing used by `arm_server` for every path; CLI shows how much it changes a saved motion. |
| `run_log.py` / `arm_trace.py` | Every arm-moving process logs itself as a Tiger Data run (with `TIGER_DATABASE_URL` or the repo `.env`); `arm_server` writes the 100 Hz trace. |

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

### Wrist camera -> containers (markers.py, deliver.py)

The 4 containers form one tray; each has an ArUco marker (DICT_4X4_50, ids 10-13 = containers a-d, **20 mm** black
square, ~30 mm apart, square to the tray line) lying flat beside it. One photo from the **scan pose** (saved in
`vision_calib.yaml`, `tray` section, not committed) finds the tray: marker centres through a homography, a rigid fit of
the tray layout (2 visible markers are enough). Calibration, learned the hard way:

- **Every scan photo under power** (`arm_server` holding the scan pose). Under load J3 sags ~2.4 deg and J4 ~1 deg,
  which moves the view ~30 mm vs. a hand-held photo.
- **Spread the calibration over the area the tray may be in.** A map fitted at one spot was 5-10 % stretched
  elsewhere (20 mm squares read 17.6-20.3 mm). `markers.calibrate_from` can also use photos of the tray at untouched
  spots: the tray's known shape (20 mm squares, layout) constrains the map there.
- The scan pose sees a band ~450-570 mm from the base; turning the base (J1) sweeps it sideways only.
- Touch markers by hand with the motors off: `calibrate touch` records by itself when the tip sits still on the table.
- `visit` / `deliver` measure the tip from the encoders after every travel and lower and correct for the sag
  (~17 mm short, ~35 mm low at the tray without it), to within 1 mm, learning it from stop to stop.
- The gripper tip is 166 mm along the tool **x** axis (not z, as `kinematics.py` says). Pointing straight down is
  out of reach that far out, so `visit` tilts the tip outward by the least that works.

### Failsafe: CAN dropouts (2026-10-24: the arm fell)

At 22:40 the CAN receive thread in `robstride_can.py` died on a USB read error (it used to re-raise and exit):
commands still reached the motors, replies didn't. `arm_server` saw "lost feedback", its return to rest was aborted
by the same check, and its "disable motors" got through: the motors let go with the arm up. Earlier dropouts killed
the whole link, so the disable never arrived and the arm stalled instead. Now:

- the receive thread survives USB read errors (counted in `bus.rx_errors`, printed);
- on lost feedback / fault / temperature / Ctrl+C, `arm_server` holds the arm with torque for 2 s waiting for the
  feedback, then eases to rest (8 deg/s on commands alone if feedback is still gone), and disables **only once at
  rest**; nothing aborts that return to rest. If it can't finish, the motors are left holding (never dropped).
  `teleop.py` likewise. Tested on a simulated bus (`tests/test_arm_server_failsafe.py`, fails on the old code).
- Likely trigger: USB. All three dropouts that day came after the wrist camera started streaming nonstop through the
  same USB-C hub (port 4) as the arm's adapters; USB selective suspend is also on. Keep the CAN adapter off the
  camera's hub and disable USB selective suspend (Control Panel -> Power Options -> USB settings).
- RobStride motors keep their last position command when commands stop arriving: a stalled arm is recoverable
  (`rest_arm.py` after replugging), a dropped one is not.
- Heat: J2 reached 81 C (limit 80) after long holds at the far scan pose; rest between runs.

## References

- Seeed wiki: [B601-RS MIT control](https://wiki.seeedstudio.com/rebot_arm_b601_rs_mit_control/), [B601-RS LeRobot](https://wiki.seeedstudio.com/rebot_arm_b601_rs_lerobot/)
- LeRobot [reBot B601 docs](https://huggingface.co/docs/lerobot/rebot_b601) (leader/follower joint mapping)
- [Seeed-Projects/lerobot-teleoperator-rebot-arm-102](https://github.com/Seeed-Projects/lerobot-teleoperator-rebot-arm-102)
