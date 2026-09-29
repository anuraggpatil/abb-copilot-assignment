#!/usr/bin/env bash
#
# Run the whole stack from one terminal: `make dev`.
#
# The four services can each be started by hand (`make api`, `make mcp`, `make backend`,
# `make frontend`) and during development that is often what you want — four windows, four logs.
# This script exists for the other case: bringing the system up to look at it, where four windows
# is four chances to forget one and then debug a copilot that cannot reach its MCP server.
#
# Two things it does that starting them by hand does not:
#
#   * **A port preflight.** A port already in use makes uvicorn print `[Errno 48] Address already
#     in use` and makes the MCP server print nothing useful at all. Neither says *what* holds the
#     port, and the usual culprit is an earlier run of this same stack. So before starting
#     anything, every port is checked and any conflict is reported with the PID and command that
#     owns it — then nothing is started, because a half-started stack is worse than none.
#   * **One Ctrl-C stops everything.** Including uvicorn's reloader children, which outlive a
#     plain `kill` of the parent often enough to be the thing holding the port next time.
#
# Ports follow the same variables as the Makefile: API_PORT, MCP_PORT, BACKEND_PORT, WEB_PORT.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

API_PORT=${API_PORT:-8000}
MCP_PORT=${MCP_PORT:-9100}
BACKEND_PORT=${BACKEND_PORT:-8080}
WEB_PORT=${WEB_PORT:-5173}
LOG_DIR=${LOG_DIR:-.logs}

# ---------------------------------------------------------------- preflight

conflict=0
check_port() {
  local port=$1 name=$2 pid
  pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)
  if [[ -n $pid ]]; then
    # Directories stripped out of the command line: every one of these is a venv interpreter with
    # a 60-character path in front of the only part that identifies it.
    printf '  port %-5s (%s) is held by pid %s — %s\n' "$port" "$name" "$pid" \
      "$(ps -o command= -p "$pid" 2>/dev/null | sed -E 's#/[^ ]*/##g' | cut -c1-90)"
    conflict=1
  fi
}

check_port "$API_PORT" "alarm API simulator"
check_port "$MCP_PORT" "MCP server"
check_port "$BACKEND_PORT" "copilot backend"
check_port "$WEB_PORT" "Vite dev server"

if ((conflict)); then
  cat <<EOF

Nothing was started. Either those are an earlier run of this stack — stop them:

  pkill -f 'uvicorn apps.alarm_api.main:create_app'
  pkill -f 'alarm_management --transport'
  pkill -f 'uvicorn apps.backend.api.main:app'
  pkill -f 'vite'

or they belong to something else, in which case move ours out of the way:

  make dev MCP_PORT=9200 BACKEND_PORT=8081

(If you change a port, the frontend needs to know: VITE_API_BASE in apps/frontend/.env.local,
and the backend needs MCP_SERVER_URL / ALARM_API_BASE_URL in .env to match.)
EOF
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "warning: no .env — the LLM has no token, so /chat will fail. cp .env.example .env first."
  echo
fi

# ---------------------------------------------------------------- start

mkdir -p "$LOG_DIR"
pids=()
names=()

start() {
  local name=$1
  shift
  "$@" >"$LOG_DIR/$name.log" 2>&1 &
  pids+=("$!")
  names+=("$name")
  printf '  %-8s pid %-6s log %s\n' "$name" "$!" "$LOG_DIR/$name.log"
}

stop() {
  # `trap -` first: the EXIT trap would otherwise re-enter this while it is killing things.
  trap - INT TERM EXIT
  echo
  echo "stopping…"
  local i
  for i in "${!pids[@]}"; do
    # Children before the parent: uvicorn --reload runs the server in a child, and killing only
    # the reloader can leave that child holding the port.
    pkill -P "${pids[$i]}" 2>/dev/null
    kill "${pids[$i]}" 2>/dev/null
  done
  wait 2>/dev/null
  echo "all four stopped."
}

# Ctrl-C is how this script is meant to end, so it exits 0 — otherwise make reports `Error 1` and
# a clean shutdown looks like a failed build. The EXIT trap is the other path: a service crashed,
# the exit status is already set, and stopping the survivors must not change it.
on_signal() {
  stop
  exit 0
}
trap on_signal INT TERM
trap stop EXIT

echo "starting four services (logs under $LOG_DIR/):"
start api uv run uvicorn apps.alarm_api.main:create_app --factory --reload --port "$API_PORT"
start mcp uv run python -m alarm_management --transport streamable-http --port "$MCP_PORT"
start backend uv run uvicorn apps.backend.api.main:app --reload --port "$BACKEND_PORT"
start web bash -c "cd apps/frontend && npm run dev -- --port $WEB_PORT --strictPort"

# ---------------------------------------------------------------- wait for ready

# Poll rather than sleep: the simulator generates its dataset on startup and the backend discovers
# the MCP catalogue, so "up" is a few seconds after "started" and varies with the machine.
# No -S: a connection refused is the expected state for the first second or two, and printing
# curl's complaint about it in the middle of the progress dots reads like a failure.
ready() { curl -fs -o /dev/null --max-time 2 "$1"; }

echo
printf 'waiting for health'
for _ in $(seq 40); do
  if ready "http://localhost:$API_PORT/health" && ready "http://localhost:$BACKEND_PORT/health"; then
    echo " ok"
    break
  fi
  # A service that died is a failure to report now, not after 20 more seconds of dots.
  for i in "${!pids[@]}"; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      echo
      echo "${names[$i]} exited during startup. Last lines of $LOG_DIR/${names[$i]}.log:"
      tail -15 "$LOG_DIR/${names[$i]}.log"
      exit 1
    fi
  done
  printf '.'
  sleep 1
done

cat <<EOF

  GUI        http://localhost:$WEB_PORT
  backend    http://localhost:$BACKEND_PORT/health
  MCP        http://localhost:$MCP_PORT/mcp
  simulator  http://localhost:$API_PORT/health

$(curl -fsS --max-time 3 "http://localhost:$BACKEND_PORT/health" 2>/dev/null || echo 'backend health unavailable')

Tailing all four logs. Ctrl-C stops everything.

EOF

tail -f "$LOG_DIR"/api.log "$LOG_DIR"/mcp.log "$LOG_DIR"/backend.log "$LOG_DIR"/web.log &
tailer=$!
pids+=("$tailer")
names+=("tail")

# Poll rather than `wait -n`: macOS ships bash 3.2, which does not have it. A crash should surface
# here, named, instead of looking like a hang behind a log tail that has gone quiet.
while :; do
  for i in "${!pids[@]}"; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      echo
      echo "${names[$i]} exited — last lines:"
      [[ ${names[$i]} != tail ]] && tail -15 "$LOG_DIR/${names[$i]}.log"
      exit 1
    fi
  done
  sleep 2
done
