# Laminar Console

Convex backend + React dashboard for the pour robot.

## Cold start

```bash
# from repo root:
cd console
npm install                     # already done if you cloned fresh
npx convex dev                  # FIRST RUN ONLY: opens a browser for login, creates a project
                                #   leave it running in this terminal — it watches convex/
```

The first `npx convex dev` writes `console/.env.local` with `VITE_CONVEX_URL` and `CONVEX_DEPLOYMENT`. Print it to see the deployment name / URL:
```bash
cat .env.local
```

In the Convex dashboard (link that `npx convex dev` prints), set:
```
ANTHROPIC_API_KEY = <your key>
```
This is needed by `convex/planner.ts` to call `claude-sonnet-4-6`.

Seed the flow table from `pour/data/pour.yaml`:
```bash
node scripts/seed.mjs
```

Start the dashboard (new terminal):
```bash
npm run dev                     # http://localhost:5173
```

## Files

### Backend (`convex/`)
| file | purpose |
|---|---|
| `schema.ts` | tables: jobs, steps, telemetry, levels, pours, events, flowTable, benchNodes + indexes |
| `requests.ts` | `submit` — user mutation that creates a `planning` job and schedules the planner |
| `planner.ts` | internal action (Node runtime) that calls Anthropic claude-sonnet, validates JSON, writes plan |
| `jobs.ts` | list, get, approve, reject, nextForRobot, setStatus, watchdog `checkStalledInternal` |
| `control.ts` | stopRequested (query), estop + clearStop (mutations) |
| `robot.ts` | pushTelemetry, pushLevels, latestTelemetry, levelsForJob |
| `steps.ts` | start/end, forJob; `start` schedules the stall watchdog 11 s out |
| `pours.ts` | record, recent, forJob |
| `events.ts` | log, recent |
| `snapshots.ts` | generateUploadUrl, attach |
| `storage.ts` | getUrl (for `<img>` src in History page) |
| `flowTable.ts` | get, update, cron-called `refitInternal` (least-squares over recent pours) |
| `bench.ts` | state (<200 bytes), approve (bench head unit), heartbeat, nodes |
| `crons.ts` | every 10 min → `flowTable.refitInternal` |

### Frontend (`src/`)
| page | what it shows |
|---|---|
| `/jobs` | submit box; recent jobs table; click a job → plan + approve/reject |
| `/live` | 6 joint gauges, pour chart (mL vs s with target line), current step, STOP button, bench node heartbeats |
| `/history` | pours table with error, before/after thumbnails |
| `/settings` | flow table editor (onsetDeg, pourDeg, rates list) |

All data via `useQuery` — no polling, Convex pushes updates.

## Robot client

Ships as `pour/robot_client.py` (uses the `convex` Python SDK). It polls
`jobs.nextForRobot`, drives each primitive via subprocess (same way `run_task.py`
does), streams `arm_status.json` to `robot.pushTelemetry` at 10 Hz, records the
final pour from `pour/logs/pours.jsonl` via `pours.record`, and aborts on
`control.stopRequested`. Run it next to the robot:

```bash
# in a third terminal (convex dev + npm run dev already running)
python pour/robot_client.py --level auto
# flags:
#   --dry-run      run every primitive with --dry-run (no motion)
#   --once         pick up one approved job and exit
#   --poll 2       seconds between polls when idle
#   --keep-server  don't stop arm_server when the client exits
```

Levels aren't streamed yet (would require `adaptive_pour.py` to push `levels` samples
during polling). The pour chart on `/live` stays empty until that's wired.

## Troubleshooting

- `VITE_CONVEX_URL is not set` in the browser console → run `npx convex dev` once in this folder.
- Planner says `ANTHROPIC_API_KEY is not set` → add it in the Convex dashboard → Settings → Environment variables.
- The seed script says `CONVEX_URL not set` → make sure `.env.local` exists (it's created by `npx convex dev`).
