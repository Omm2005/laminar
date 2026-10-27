# Laminar

**No spill. No splash.** Laminar is an adaptive, camera-guided liquid-handling system built around an affordable general-purpose robot arm. It picks up a vial, moves it without sloshing, and pours toward a target volume while a camera measures the liquid in real time.

Built for **Cal Hacks 13.0** with a working lab, Laminar explores whether adaptable lab automation can be made with a roughly $2k arm, cameras, and software rather than a fixed, five- or six-figure liquid handler.

[Watch the demo](https://devpost.com/software/laminar-ma1h69) · [Explore the project story](https://devpost.com/software/laminar-ma1h69)

## What it does

- Finds a tray's position from ArUco markers, so the station can move without a fixed deck setup.
- Uses a custom liquid-level vision model to estimate delivered volume from camera images.
- Controls a six-joint Seeed reBot B601-RS arm, with a custom RobStride CAN driver, kinematics, and measured gravity/friction compensation.
- Pours with feedback from measured liquid level, stopping on target or aborting for spill risk, camera loss, or no-flow conditions.
- Provides a live dashboard for the arm, cameras, run status, and telemetry, with run traces logged to Tiger Data.
- Supports typed, chat, and push-to-talk requests. AI can propose and explain a plan, but plain-code checks and a named human approval gate execution.

A demonstrated run camera-controlled a **3.4 mL pour** and stopped as liquid approached the neighboring channel. The liquid-level model reached about **0.26 mL mean error** in the project tests. Performance and reliability still need validation across more pours, liquids, and setups.

## System overview

1. A technician submits a protocol or asks for a pour.
2. The system resolves the tray and channel locations and creates a proposed run.
3. A deterministic code checker validates the plan; a named person reviews and approves it.
4. The robot executes the taught transfer and feedback-controlled pour.
5. Cameras measure the result, while the dashboard and audit log report the run.

**Safety boundary:** Claude and other agents do not directly control the arm. Requests from chat, voice, or external agents go through the same checks and human-approval gate. Hardware faults are designed to leave the arm holding position rather than dropping its load.

## Repository map

| Folder | Contents |
| --- | --- |
| mech/ | Mechanical work |
| robotics/ | Arm control, motion, and teleoperation |
| sense/ | Camera and liquid-level sensing |
| software/ | Agent and software components |
| laminar-web/ | Live dashboard and web app |
| voice/ | Voice interaction |
| HANDOFF/ | Project handoff notes |

For setup and component-specific instructions, see the README files in the relevant folders. In particular, [software/README.md](software/README.md) documents the Fetch.ai agent.

## Built with

Python, NumPy, OpenCV, ONNX, python-can, TypeScript, Convex, PostgreSQL/TimescaleDB (Tiger Data), Cartesia, Claude, Fetch.ai uAgents, and Mana.

## Demo and links

- [Laminar on Devpost](https://devpost.com/software/laminar-ma1h69)
- [Mana Lab Console](https://www.mana.space/home/FFIzXyDYQwYiO0Q11ujq7sfya3A3/apps/laminar-lab?chatId=001792899827861bd784ede37a045cc9bfe)
- [Try the Fetch.ai agent in ASI:One](https://asi1.ai/invite?channelInviteKey=pt4JtuNel3NcRDi9tboMdn8FRmM-ZlFLqtr67PXQo30&utm_source=share)

## What we learned and next steps

Precision can come from measuring the output, not only from expensive mechanics. We kept repeatable transport motions simple and closed the loop around the pour, where variation matters most. Next we want to test repeatability across more volumes and liquids, improve reliability as camera or tray positions change, and make new labware and protocols easier to configure.
