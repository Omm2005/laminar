# Laminar web

The landing page and the technician console for Laminar, the reBot arm that pours to ±0.2 mL. Everything is a
TypeScript app on [Convex](https://convex.dev): the pages subscribe to live queries, and the robot, the voice loop,
the Mana Lab Console and Tiger Data all meet in one Convex backend.

| Page | File | What it does |
| --- | --- | --- |
| Landing | `index.html` + `src/landing-live.ts` | The team's landing page. Its 3D arm, delivered-volume counter, phase list, camera tiles and console stats are fed by Convex live queries. The arm switches from its replay loop to the live stream by itself while a run is going or the real arm is publishing. |
| Console | `console.html` + `src/` | Run (ask in words or by voice, approve by name, 3D arm, both cameras), Protocol builder, Run log & QC, Reports, Audit, Beat the robot, Tiger Data. |

## Run it

```bash
npm install
npx convex dev          # first run: log in and create a deployment; writes .env.local
npm run dev             # http://localhost:5173 (landing) and /console.html
```

Set these on the Convex deployment (`npx convex env set NAME value`), never in the repo:

| Variable | Needed for |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude plans free-form requests and writes the end-of-run note. Without it a rule parser plans. |
| `CARTESIA_API_KEY` | Voice on the Run tab (speech-to-text and text-to-speech). |
| `TIGER_DATABASE_URL` | Tiger Data analytics and the arm-run charts. |
| `BENCH_TOKEN` | Lets the bench laptop publish joints and camera frames. |
| `MANA_SUBMIT_TOKEN` | Lets the Mana Lab Console send runs. |

## Convex

| Feature | Where |
| --- | --- |
| Live queries | Every console tab uses `useQuery`. The landing page subscribes with `ConvexClient.onUpdate` to `site.liveArm`, `site.liveStats` and `robot.liveFrame`: camera JPEGs are pushed as bytes over the websocket, one document per camera overwritten in place. |
| Workflows (component) | `convex/exec.ts`: each assay is a durable `@convex-dev/workflow`, one step per move. Stop cancels the workflow; Claude's run note is a retried step. |
| Rate limiter (component) | `convex/limits.ts`: per-operator submits, the Mana endpoint and voice. |
| Mutations and the checker | `convex/runs.ts`, `convex/lib.ts`: requests become runs; plain code checks every plan before it is read back; a named person approves. |
| Actions | `convex/planner.ts` (Claude), `convex/voice.ts` (Cartesia), `convex/tiger.ts` (Postgres). |
| Crons | `convex/crons.ts`: 08:00 QC report, telemetry and voice-cache cleanup. |
| File storage | Protocol CSV uploads, camera snapshot history (one every 3 s), cached synthesized speech. |
| Search | Full-text search over the audit log and protocol names (`convex/search.ts`). |
| HTTP actions | `convex/http.ts`: the bench (`/bench/telemetry`, `/bench/frame`, `/bench/next`, `/bench/update`), the Mana Lab Console (`/mana/summary`, `/mana/submit`) and the public site snapshot. |

## Cartesia

Voice on the Run tab, server side only (the key never reaches the browser).

- **Ink speech-to-text** (`ink-whisper`): hold the mic, speak, release. The clip is transcribed in a Convex action and never stored. Silence artefacts ("thank you", "um") are dropped.
- **Sonic text-to-speech** (`sonic-3.6`): reads back the checked plan, "Approved. Starting now.", each pour and the result. Alerts are faster and louder. Each phrase is synthesized once and cached in file storage.
- Commands: a request, `approve`, `cancel` / `don't approve`, `repeat`, `status`, and `stop` (anywhere in a sentence, interrupts speech and stops the run).
- Safety: the read-back is built from the checked plan, never from model text; `approve` only works while a plan is waiting and is logged as "<name> (voice)".
- Limits: push-to-talk, not always listening; a stop cannot interrupt a move already running on the real arm (the hardware e-stop can); needs microphone permission in the browser.

## Tiger Data

`convex/tiger.ts` writes every pour and audit event to hypertables with a real-time continuous aggregate and
compression, and reads the team's `arm_runs` / `arm_trace` (100 Hz joint traces) with `run_smoothness()` and
`run_trace()` for the Tiger Data tab: commanded vs measured per joint, smoothness per joint, and a replay on the 3D arm.

## Mana

The Laminar Lab Console, built in Mana, polls `/mana/summary` (jobs, pours with liquid labels, 12 channels) and sends
plans to `/mana/submit`. Those runs show up on the console marked "from Mana" and still need a named approval.

## The bench

`bench/publish.py` runs on the robot laptop. It never opens a camera and never talks to the arm: it reads
`robotics/arm_status.json`, `robotics/wrist_latest.jpg` and `robotics/side_latest.jpg` (each camera has exactly one
owner process, which writes its newest frame there) and posts them to Convex over persistent connections.

```bash
BENCH_TOKEN=... python bench/publish.py --robotics ../robotics
```

Measured end to end through Convex (frame written → page has it): about 210 ms median at 8–11 fps.

## Known limitations

- Without the bench publishing, the console runs a simulated bench (labelled "simulated" everywhere). Its arm poses
  come from the team's taught poses in `robotics/spots.yaml`; lift and tilt offsets are approximations.
- Operator names are typed, not verified accounts.
- The console's simulated bench uses four wells; the robot and the landing page use the 12-channel reservoir.
