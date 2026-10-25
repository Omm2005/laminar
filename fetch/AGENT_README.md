# Laminar Bench: a remote wet-lab robot you can ask to pour

![domain:lab-automation](https://img.shields.io/badge/lab--automation-3D8BD3) ![tech:robotics](https://img.shields.io/badge/robotics-0B3D91) ![tech:computer-vision](https://img.shields.io/badge/computer--vision-6E44FF) ![tech:llm](https://img.shields.io/badge/llm-FF6F00)

**Laminar Bench** lets another lab use a real liquid-handling robot. You ask in plain words, and a robot arm in our lab
pours measured volumes of liquid into containers. A camera reads how much actually went in, and you get the measured
volumes back.

**Use it for:** remote lab automation, liquid handling and dispensing, pouring or pipetting a measured volume,
dilution series, dispensing QC, wet-lab experiments run by a robot arm, and camera-measured fill levels.

## What it does

- **Understands lab requests**, for example "pour 1 mL of the blue dye into container 2", "0.5, 1 and 1.5 mL of red
  into containers 1, 2 and 3", "what's on the bench?" or "why did my last run stop?".
- **Plans and checks against the real bench.** It checks which vials and liquids are loaded, how much is left, how
  full each container is, whether the arm can reach it and whether it would overflow. Impossible requests are refused
  with the reason.
- **Puts a person in the loop.** The plan appears as a permission card on the robot's dashboard, and nothing moves
  until the technician approves it.
- **Runs it on the robot.** The arm picks the vial, carries it to the container and pours, while a camera-trained level
  model steers the wrist and stops at the target. It then carries the vial back.
- **Reports the result:** the measured mL for each container, the error against the target, and why it stopped if it
  did. A full audit trail is kept.

## How to use it

1. Ask: `Pour 1 mL of the blue dye into container 2 and tell me what the camera measures.`
2. You get the checked plan. On the **real arm**, the technician approves it. On the **simulated bench** (demo mode),
   reply `approve` yourself.
3. Ask `status` after about a minute: you get the measured volume per container and the error against the target.

Commands: `approve` (simulated bench only), `cancel` (withdraws your waiting plan), `status` (your last runs and
results).

Other agents can use it too. Send a `MetadataContent` with `{"lab": "<your lab name>"}` to get machine-readable
updates (`plan_waiting`, `approved`, `run_finished` with results).

## Example prompts

- What liquids and containers are on the Laminar bench right now?
- Pour 1 mL of the blue dye into container 2 and tell me the measured volume.
- Run a dilution check: 0.5 mL, 1 mL and 1.5 mL of red dye into containers 1, 2 and 3.
- Why did my last run stop?

Safety: only a person at the bench can approve runs on the real arm. Volumes are limited to 5 mL per pour, and a pour
never fills a container past 97 % of its rim.
