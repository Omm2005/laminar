# Voice

Hands-free control for gloved lab techs, built on Cartesia: speak a request, hear the plan read back,
say "approve", and hear progress and the result while the arm works.

```
mic → Ink (speech-to-text) → planner + safety checks → Sonic read-back → "approve"
    → arm runs → Sonic callouts while pouring ("half full… almost there… done") → spoken report
```

## Layout

| File | What it is |
| --- | --- |
| `voice.py` | Cartesia speech in (`Listener`, Ink) and out (`Speaker`, Sonic), intent parsing, read-back, pour callouts, the conversation loop |
| `integration.py` | Connects voice to the rest of Laminar. Three backends: `fake`, `om` (software/), `arm` (robotics/ + sense/) |
| `requirements.txt` | `cartesia`, `sounddevice`, `numpy` |

## Safety rules

- Nothing moves without a spoken "approve" (or the dashboard) **after** the plan is read back.
- The read-back is built from the validated plan JSON, never from free model text: what you hear is what runs.
- "stop" / "abort" / "halt" anywhere in a sentence stops at once, in any state, no wake word needed.
  Software stop only: the hardware e-stop stays the primary stop.
- Plans are checked by code before the read-back (volume, column, vial, route, channel capacity).
- Audio is never saved; only transcripts go to the log. The mic is ignored while the speaker talks.

## Setup

```bash
cd ~/laminar && source .venv/bin/activate
pip install -r voice/requirements.txt
export CARTESIA_API_KEY=...        # from play.cartesia.ai/keys; never commit it
```

macOS asks once for microphone access for Terminal: allow it. Use headphones or a quiet speaker.

## Run it

```bash
cd voice
python integration.py --mock                                # fake robot, type instead of speaking (no API)
python integration.py                                       # fake robot, real mic + Cartesia
python integration.py --mock --backend om                   # Om's planner, validator and executor (fake arm)
python integration.py --mock --backend arm --dry-run        # real layout + motion planning, nothing moves
python integration.py --backend arm --cam 2                 # the real arm and side camera
```

Things to say:

| Say | Laminar |
| --- | --- |
| "Pour 1 ml of blue into column C" | reads the plan back, asks for approve |
| "approve" / "cancel" | runs it / "Cancelled. Nothing moved." |
| "status" | "Pouring column C, 60 percent." |
| "repeat" | reads the plan again |
| "stop" | stops now, any time |

`--wake laminar` ignores everything that doesn't contain "laminar" (except "stop"), for noisy rooms.

## Backends

| Backend | Planner + checks | Runs on | Progress from |
| --- | --- | --- | --- |
| `fake` | built-in | fake robot | built-in |
| `om` | `lab_agent.parse_request` (`--claude`: Claude) + `build_plan`, `validate_plan` | `Executor` on `make_system()` | telemetry events (`executor.step`, `pour.sample`) |
| `arm` | built-in, from `robotics/layout.yaml` | `transfer.py` moves + `pour_motor6` camera pour | the pour's `on_tick` |

**`arm` needs:**
- `arm_server.py` running, with the arm already holding the vial (the grab is a recorded step).
- The routes measured in `layout.yaml`.
- The `vials:` list in `layout.yaml`, saying which liquid each vial holds.
- One box per compartment on the side camera: `cd sense && python live_view.py --cam 0 --new-boxes` (A, B, C, D left to right). No calibration: the level model reads the boxes.

On "stop" it tilts the vial upright and **holds where it is**; a move already running finishes first.

## Settings (environment variables)

| Variable | Default |
| --- | --- |
| `CARTESIA_API_KEY` | required except with `--mock` |
| `CARTESIA_VOICE_ID` | Cal Hacks starter voice; pick one at play.cartesia.ai/voices |
| `CARTESIA_TTS_MODEL` / `CARTESIA_STT_MODEL` | `sonic-3.6` / `ink-whisper` |
| `CARTESIA_SPEED` | `1.0` (0.6 – 1.5) |
