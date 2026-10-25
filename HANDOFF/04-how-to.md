# 04 - How to do things (exact commands)

All from `C:\Users\amogh\Downloads\laminar-main\robotics` unless noted. Git Bash paths shown.

## Before anything
```bash
# processes that should be running for the dashboard (leave them alone):
#   python -u publish.py           (robotics/)  -> website
#   python -u wrist_stream.py --camera 1          -> robotics/wrist_latest.jpg
#   python -u live_view.py --cam 2 --no-record   (sense/) -> side camera + robotics/side_latest.jpg
python -I -c "import psutil;print([' '.join(p.info['cmdline'][1:]) for p in psutil.process_iter(['cmdline']) if p.info['cmdline'] and p.info['cmdline'][0].lower().endswith('python.exe')])"
powershell -NoProfile -Command "Get-PnpDevice -PresentOnly | ? { \$_.FriendlyName -match 'candle|CH340' }"   # adapter + leader present?
```
If the candleLight adapter is missing -> ask the user to replug it (directly into the laptop). Don't open the adapter
from two processes back to back.

## Start the arm server (and check it)
```bash
rm -f ../../srv.log arm_cmd.json STOP
(python -u arm_server.py > ../../srv.log 2>&1 &)
for i in $(seq 30); do sleep 0.5; grep -q "arm_server ready\|STOPPED\|Error" ../../srv.log && break; done
sleep 3; grep -v "^cmd\|tiger" ../../srv.log | tail -3            # must say ready, no STOPPED
python -I -c "import sys;sys.path.insert(0,'.');from arm_client import ArmClient;c=ArmClient();print(c._send({'op':'gripper','target':20,'speed':60.0}).get('result'));print(c._send({'op':'gripper','target':0,'speed':60.0}).get('result'))"
# gripper must read ~19 then ~0. If it stays ~0.3: gripper limp (see 02 quirk 3).
```
Use `set -o pipefail` / explicit checks: a failed step must stop the chain (a `| grep` hides the exit code).

## Stop NOW (user says "stop")
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -match 'pour_episode|arm_server' } | % { Stop-Process -Id $_.ProcessId -Force }
```
Motors keep holding the last command (arm frozen, not dropped). Then `rm -f arm_cmd.json`, read the pose with
`python detect_arm.py`, and restart live_view if pour_episode was killed (it borrowed the side camera):
`cd ../sense && powershell -NoProfile -Command "Start-Process python -ArgumentList '-u','live_view.py','--cam','2','--no-record' -WorkingDirectory '<sense dir>'"`.

## Back to rest / motors off
* From a holding arm with no server: `python rest_arm.py` (only near rest or with the arm supported).
* With arm_server running: `python arm_cmd.py rest` (eases to rest, disables, server exits).
* Vial still in the gripper away from rest: return it first (see "recover a held vial").

## Teleop
```bash
python calibrate_leader.py --yes        # leader physically at rest first
rm -f STOP && (python -u teleop.py --hz 100 --keep-grip --smooth-ms 100 > ../../teleop.log 2>&1 &)
# with recording: add --record --fps 20 --camera 1 (camera 1 must not be owned by wrist_stream -> stop it first)
touch STOP                               # graceful stop (arm to rest, MP4 finalized)
```
Teleop refuses to start if leader and follower differ by >25 deg: put the leader at rest / recalibrate.
"Save the pose for letter X" during teleop: parse the last status line of the teleop log (`J1=... J7=...` are the
commanded follower angles) and `spots.save_spot("vial_X_final_position", q)`. A helper doing exactly this:
```python
import re, sys; sys.path.insert(0, r"...\robotics"); from spots import save_spot
last = open(log).read().split("\r")[-1]
q = {int(j): float(v) for j, v in re.findall(r"J(\d)=\s*(-?\d+\.\d)", last)}; save_spot("vial_A_final_position", q)
```

## Record a vial grab path (rest -> vial)
Teleop with `--record`; at the vial (gripper closed on it) the user says the letter:
`python grab.py save vial_A --rec recordings/<run> --frame <last row index> --label "..."` (frame = data.csv rows - 2).
Then stop teleop with STOP, trim the recording to frame+1 rows and the video to the same frame count (cv2 re-encode),
rename the folder `vial_A_<timestamp>`, fix `SOURCE` in `paths/vial_A.py`, commit + push `paths/vial_A.py` + `spots.yaml`.

## Learn container offsets (tray must not move between teaching and this)
`python deliver.py learn-offsets --run --camera 1` (arm_server running) - scans and rewrites ALL four containers from
the current `vial_X_final_position`. To keep one container's old offset, merge in Python (see 05, 03:38 entry).
Without moving the arm, from a saved scan-pose photo: `markers.find_tray(cv2.imread(photo), calib, 0.0)` then
`deliver.learn_offsets(pose)` / `deliver.save_offsets(offsets, pose)`.

## Run episodes
```bash
python -u pour_episode.py A2b --run                     # fill b to --target (0.9 of full)
python -u pour_episode.py A2b:50 B2b:+50 --run          # sequence; 100 % = 95 % of the box
python -u pour_episode.py C2a --run --dry-tilt --max-tilt 150    # no vision: tilt to max and back (empty vial)
```
Watch with a Monitor on the task output for `\[phase\]|pour:|STOPPED|NOT|Error|Traceback|episode`. After: read
`logs/episode_*/summary.json`, `levels.csv` (raw_ml for real liquid), `pour_guide.jsonl`, and look at `side/` and
`wrist/` frames before reporting. The arm ends at rest with arm_server still running: `python arm_cmd.py rest`.
Plan-only (no motion): `python deliver.py A2b --image <scan photo>`.

## Recover a held vial (episode died mid-way, vial in gripper, arm_server running)
Lift straight up to the carry plane, travel over the slot with the grasp orientation, lower to the grasp pose, then
`grab.play(vial, run=True, reverse=True)`; a ready script was in the scratchpad (`recover_vial.py vial_C`) - logic:
`plan_line(q,[p.x,p.y,pa.z+150])`, `plan_line(...,[pa.x,pa.y,z_carry],Ra)`, `plan_line(...,pa,Ra)`.

## Commit + push
```bash
cd /c/Users/amogh/Downloads/laminar-main
git add <files>
git -c user.name=AmoghShrivastava -c user.email=amoghshri1601@gmail.com commit -q -m "...

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git pull -q --rebase && git push -q origin main && cd ../laminar-clone && git pull -q --ff-only
```
