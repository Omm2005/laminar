#!/usr/bin/env bash
# Start the whole Laminar dashboard on this Mac with one command:
#
#     console/start.sh            # side camera 0, the real arm
#     console/start.sh 1          # another side camera
#     console/start.sh sim        # simulated arm + camera (no hardware: try it anywhere)
#
# 1. the dashboard's data store (Convex, running locally on this Mac: no account, nothing to set up)
# 2. your Claude key from the repo .env (or ~/laminar/.env), handed to it (the key is never printed)
# 3. the dashboard website              -> http://localhost:5173
# 4. the bench link: arm (arm_server.py), side camera, level model, approved plans
# Ctrl+C stops everything. Logs: ~/laminar/logs/dashboard/*.log
#
# The robot arm itself is started separately (it holds the arm):
#     cd ~/laminar/robotics && ../.venv/bin/python mac_run.py arm_server.py --hold-grip
set -u
CAM="${1:-0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${LAMINAR_REPO:-$(cd "$HERE/.." && pwd)}"
LOGS="$REPO/logs/dashboard"
PY="${LAMINAR_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$HOME/laminar/.venv/bin/python"
[ -x "$PY" ] || { echo "no Python venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r sense/requirements.txt"; exit 1; }
ENV_FILE="$REPO/.env"; [ -f "$ENV_FILE" ] || ENV_FILE="$HOME/laminar/.env"
mkdir -p "$LOGS"
cd "$HERE" || exit 1

stop_old() {   # anything from an earlier start (or still running) on our ports
  pkill -f "bridge/run_plan.py" 2>/dev/null
  pkill -f "convex dev" 2>/dev/null
  for port in 5173 3210 3211; do lsof -ti ":$port" | xargs kill 2>/dev/null; done
  sleep 1
}
pids=()
cleanup() {
  echo; echo "stopping the dashboard..."
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
  stop_old
  echo "stopped."
}
trap cleanup EXIT INT TERM

stop_old
command -v bun >/dev/null || { echo "bun is not installed: brew install oven-sh/bun/bun"; exit 1; }
[ -d node_modules ] || bun install

echo "1/4 data store (local)..."
# use the backend already downloaded on this Mac (no internet needed); download only if there is none yet
CACHED="$(ls -1t "$HOME/.cache/convex/binaries" 2>/dev/null | head -1)"
VERSION_ARGS=()
[ -n "$CACHED" ] && VERSION_ARGS=(--local-backend-version "$CACHED")
CONVEX_AGENT_MODE=anonymous bunx convex dev --tail-logs disable "${VERSION_ARGS[@]}" >"$LOGS/convex.log" 2>&1 &
pids+=($!)
for _ in $(seq 1 90); do grep -q "Convex functions ready" "$LOGS/convex.log" 2>/dev/null && break; sleep 1; done
grep -q "Convex functions ready" "$LOGS/convex.log" || { echo "data store did not start: see $LOGS/convex.log"; exit 1; }

echo "2/4 Claude key..."
KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]"'"'")"
if [ -n "$KEY" ]; then
  bunx convex env set ANTHROPIC_API_KEY "$KEY" >/dev/null 2>&1 && echo "    key loaded from $ENV_FILE" \
    || echo "    could not load the key (see $LOGS/convex.log); the chat will use its simple fallback"
else
  bunx convex env remove ANTHROPIC_API_KEY >/dev/null 2>&1
  echo "    no ANTHROPIC_API_KEY in $ENV_FILE: the chat uses its simple fallback, not Claude"
fi
unset KEY

echo "3/4 website..."
bun run dev --port 5173 >"$LOGS/web.log" 2>&1 &
pids+=($!)

echo "4/4 bench link (camera $CAM)..."
if [ "$CAM" = "sim" ]; then BENCH_ARGS=(--sim); else BENCH_ARGS=(--cam "$CAM"); fi
LAMINAR_REPO="$REPO" "$PY" -u bridge/run_plan.py "${BENCH_ARGS[@]}" >"$LOGS/bench.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 30); do curl -s -o /dev/null http://localhost:5173/ && break; sleep 1; done
open "http://localhost:5173/lab" 2>/dev/null
echo
echo "Laminar dashboard: http://localhost:5173   (Ctrl+C here stops it)"
echo "Placeholders (pick / put back) print here and do not move the arm:"
tail -n 0 -F "$LOGS/bench.log" 2>/dev/null | grep --line-buffered -E "placeholder|== plan|->|refused|error|Error"
