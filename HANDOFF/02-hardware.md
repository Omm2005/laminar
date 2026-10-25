# 02 - Hardware

## Follower arm: Seeed reBot Arm B601-RS
* 6 joints + gripper, RobStride motors on CAN @ 1 Mbps. J1-J3 RS06 (kp 150/kd 10 for J2/J3, J1 overridden to 120/5),
  J4-J6 + gripper J7 RS00. Host id 0xFD. 48 V supply.
* USB-CAN adapter: **candleLight gs_usb** ("candleLight USB to CAN adapter" + "firmware upgrade interface" in Device
  Manager). Python: `robotics/robstride_can.py` (`RobStrideBus`).
* Joint frame: degrees relative to `robotics/follower_calibration.json` (zero pose = folded rest). Rest = all ~0;
  J6 raw reads ~+-180 after power-up (single-turn wrap, handled).
* Limits (motion.py): J1 +-145, J2 0..170, J3 0..180, **J4 -80..90**, J5 +-90, **J6 +-90** (the pour/roll joint), gripper 0..345.
* Tool frame (motion.fk_mm): tip 166 mm along tool **x** (= gripper approach = **wrist-roll axis**); the vial's axis is
  tool **z** (vertical when upright). Rolling J6 leaves the tip in place.

## Leader arm: FashionStar StarArm 102
* Servos 0-6 on a CH340 USB-serial (**COM18** currently; it changes on replug; `leader_arm.find_leader_port()` finds it).
* Calibrate (set every servo's origin at the current pose) with `python calibrate_leader.py --yes` **with the leader
  in its rest pose**. It drifts: the base servo (id 0) slips by ~6-27 deg or even ~180 deg; the gripper zero drifts.
  Symptoms: teleop refuses ("leader and follower poses differ by J1 ... deg"), or the gripper "doesn't work" (a gripper
  zero below the clamp makes teleop command 0). Fix = recalibrate at rest. The user asks for this often.

## Cameras (DirectShow indices; they SHIFT after replugs - check with snapshots)
| Index | Camera | Use |
| --- | --- | --- |
| 0 | HP 5MP laptop webcam | - |
| 1 | **Innomaker U20CAM-1080p-S1 (wrist camera)** | tray scan (markers), wrist feed, recording |
| 2 | **icspring camera (side level camera)** | sees the 12-channel reservoir from the side; level model input |
| 3 | phone virtual camera (S25 via Link to Windows) | - |
* Side camera: opens 1920x1080 ("ultrawide") by default, 1280x720 ("wide") during pours (`sense/cameras.py` MODES).
  Level boxes are drawn in `sense/live_view.py` and saved in `sense/live_boxes.json` with their resolution; scaled on load.
  The side camera appears to move with the tray (the framing stayed the same when the tray moved). A teammate cropped
  its view to the tray (live_view `tray_view`).
* Windows allows ONE process per camera. The dashboard feed processes own them (see 03); `pour_episode.py` borrows the
  side camera from live_view and gives it back; markers.py/servo.py read the wrist camera via `robotics/wrist_latest.jpg`
  written by `wrist_stream.py` (10 fps).

## Quirks found (all real, all reproduced)
1. **CAN adapter drops off USB / wedges.** It has disappeared from Device Manager 3+ times tonight ("Cannot find
   device 0"); the user replugs it. Opening it in one process and immediately again in another crashed libusb
   ("access violation") and left it answering "Access denied" until replugged -> only one process may open it, never
   open/close it right before arm_server opens it. Prefer a USB port directly on the laptop (the hub also carries the
   cameras; last night's CAN dropouts followed nonstop wrist streaming on that hub).
2. **arm_server "lost feedback from J1/J3/J6" at startup** was a software race (feedback checked on the first tick
   after enabling); fixed in `1aa1dff` (1 s startup grace). A standalone 100 Hz hold test showed the bus healthy
   (<55 ms gaps). Mid-run lost feedback (e.g. J2/J3/J6 on Oct 24) was loose cabling (J2-J3, wrist J5-J6-J7 connectors).
3. **Gripper limp**: the gripper motor can drop out of run mode and ignore commands (0 torque, no fault). arm_server
   now re-enables any motor not in mode 2 (`251c7b4`) and refuses to start until all run. On Oct 25 ~02:50 it also
   ignored commands while in mode 2 with no fault; driving it directly (enable + MIT) once brought it back. Always do a
   gripper open/close check (target 20 -> must read ~19) before an episode.
4. **J2 overheats** holding stretched poses: teleop stopped at **81 C** (limit 80) while teaching pour poses. Rest the
   arm between long holds; teach pour poses quickly.
5. **Sag**: under load the tip hangs 20-40 mm below the commanded point at far/stretched poses (more at carry height
   than at the pour pose). deliver measures it from the encoders and compensates (see 03).
6. **Laptop on battery** throttles timing: teleop loop dropped from ~94 Hz to 48-92 Hz and felt "choppy". Plug in the
   charger. (Battery was still discharging, 77-82 %, at the end.)
7. RobStride motors keep their last command when commands stop: killing arm_server mid-motion FREEZES the arm in
   place (holding), it does not drop. That is how "stop" is implemented.
