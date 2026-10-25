# 01 - Project, goals, team, and the user's rules

## The project
**Laminar** (Cal Hacks 13, Oct 23-25 2026): a lab-automation demo. A robot arm moves liquid between vials and a
multi-channel reservoir, with a vision model measuring how full each channel is and closing the loop on the pour.
Around it: a web console/dashboard (Convex), agents (Fetch.ai / ASI:One, a planner), telemetry in Tiger Data (Timescale).

* Vials: 4 conical centrifuge tubes in a rack, **vial A ("blood", red-dyed water) B, C, D**. Each has a recorded teleop
  grab path (rest -> vial, gripper closes) and an "initial position".
* Tray / reservoir: a clear 12-channel reservoir. **Containers a-d = channels 1, 4, 7, 10** (every third channel); the
  channels between are watched for liquid going astray. **ArUco markers 10, 11, 12, 13 (DICT_4X4_50, 20 mm squares)**
  sit on the tray's front edge, one per container (10 = a ... 13 = d), ~30.4 mm apart (3 channels = ~10.1 mm each).
* Job names: **`A2b`** = vial A into container b. With a fill: `A2b:50` = fill b to 50 %; `B2b:+50` = add 50 %.
  100 % = 95 % of the camera's column box (user's rule, so 100 % never overflows); every % scales by 0.95.

## Goals (in the user's words, latest first)
* "I want A2b and B2b to run perfectly ... such that it works with any tray position in the acceptable range."
  "Any accommodation should be on the offset from the marker. I'm not taping it [the tray], that makes the point of
  ArUco useless." The tray WILL move between runs.
* Sequences like `A2b:50 B2b:+50` (mix two vials in one container by percent), controlled by the vision model.
* Dashboard streaming (cameras + telemetry to the website) must keep working while the arm runs.
* Recordings for the Microsoft track demo were saved (see 05).

## The team
* **Amogh** (the user) - robotics/arm. GitHub `AmoghShrivastava`.
* **Om Shah** - vision/level model (`sense/`), `software/laminar/pour_motor6.py`, branch `om/pour-fixes` (merged).
* **Hema Dassani** - console/web (Convex).
* Other sessions/agents also commit to `main` (website, publish.py, side-camera crop). Pull before you push.

## The user's rules (follow exactly)
* **Git identity:** commit as `AmoghShrivastava <amoghshri1601@gmail.com>`. **Never** the Purdue email.
  Use `git -c user.name=AmoghShrivastava -c user.email=amoghshri1601@gmail.com commit ...`.
  End commit messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
* **Push code to `Omm2005/laminar` only.** "Don't upload any data": recordings, calibration files, logs, `.env` stay
  local (they are gitignored). Labelled path files (`robotics/paths/*.py`), `spots.yaml`, `container_offsets.yaml`,
  `vial_offsets.yaml` ARE pushed (the user asked).
* "Don't ask me, just push it after completion and commit." Push when a piece of work is done.
* "Don't spend any Claude credits" (no Claude API calls from code you run), "don't pay for any credits".
* Never send the user's email anywhere. The Tiger DB URL/password is only in the gitignored `software/.env`.
* Also update `~/Downloads/laminar-clone` (`git pull --ff-only`) after pushing ("push everywhere").

## How the user works / what they expect
* Short messages ("go", "cook", "stop", "a", "b"). "stop" = stop the arm NOW (see 04: kill the episode + arm_server;
  the motors then hold their last command, the arm does not fall).
* They are safety-sensitive about the arm falling ("we can't have stuff like this happen"). Never disable motors with
  the arm away from rest.
* They want reports of what went well / badly after each run, with the logged numbers, not guesses. Look at the
  logged levels (`levels.csv`, `pour_guide.jsonl`) and the camera frames before claiming anything.
* They get frustrated by repeated hardware flakiness and by fixes that don't stick; be direct about causes.
* They do not trust "calibration" sessions that need them to touch things; prefer automatic methods. They are fine
  with the arm moving the camera around as much as needed.
* When they say a letter during teleop ("a", "B", "d"), they mean "save the pose for that letter now".
