#!/usr/bin/env bash
set -Eeuo pipefail

# MindBox local service launcher for macOS/Linux.
# restart/reset only replaces service processes; it never deletes user data.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/code/backend"
DEVICE_DIR="$ROOT_DIR/code/device/k230"
WEB_DIR="$ROOT_DIR/code/webui"
RUN_DIR="$BACKEND_DIR/var/run"
LOG_DIR="$BACKEND_DIR/var/log"
BACKEND_PY="$BACKEND_DIR/.venv/bin/python"
DEVICE_PY="$DEVICE_DIR/.venv/bin/python"
WEB_NPM="${NPM_BIN:-npm}"
SPAWN_PYTHON="${SPAWN_PYTHON_BIN:-python3}"

BACKEND_PORT="${MIND_BOX_BACKEND_PORT:-8787}"
BRIDGE_PORT="${MIND_BOX_BRIDGE_PORT:-8765}"
WEB_PORT="${MIND_BOX_WEB_PORT:-5173}"
WAKE_WORD="${MIND_BOX_WAKE_WORD:-你好小盒}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

say() { printf '[mindbox] %s\n' "$*"; }
fail() { printf '[mindbox] ERROR: %s\n' "$*" >&2; exit 1; }

port_pids() {
  local port="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
}

stop_port() {
  local port="$1" pid
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    say "stopping process $pid on port $port"
    kill "$pid" 2>/dev/null || true
  done < <(port_pids "$port")

  for _ in {1..20}; do
    [[ -z "$(port_pids "$port")" ]] && return 0
    sleep 0.1
  done

  while read -r pid; do
    [[ -z "$pid" ]] && continue
    say "force stopping process $pid on port $port"
    kill -KILL "$pid" 2>/dev/null || true
  done < <(port_pids "$port")
}

stop_all() {
  stop_port "$WEB_PORT"
  stop_port "$BACKEND_PORT"
  stop_port "$BRIDGE_PORT"
  rm -f "$RUN_DIR"/*.pid
}

require_file() {
  [[ -x "$1" ]] || fail "missing executable: $1"
}

start_process() {
  local name="$1" cwd="$2" pidfile="$RUN_DIR/$1.pid"; shift 2
  command -v "$SPAWN_PYTHON" >/dev/null 2>&1 || fail "python3 was not found; set SPAWN_PYTHON_BIN"
  say "starting $name"
  "$SPAWN_PYTHON" - "$cwd" "$LOG_DIR/$name.log" "$@" >"$pidfile" <<'PY'
import subprocess
import sys

cwd, log_path, *command = sys.argv[1:]
with open(log_path, "ab", buffering=0) as log:
    child = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print(child.pid)
PY
}

wait_http() {
  local name="$1" url="$2"
  for _ in {1..50}; do
    if curl --silent --show-error --fail --max-time 1 "$url" >/dev/null 2>&1; then
      say "$name ready: $url"
      return 0
    fi
    sleep 0.2
  done
  say "$name did not answer yet; inspect $LOG_DIR/$name.log"
}

start_all() {
  require_file "$BACKEND_PY"
  require_file "$DEVICE_PY"
  command -v "$WEB_NPM" >/dev/null 2>&1 || fail "npm was not found; set NPM_BIN or install Node.js"

  local wake_args=(--wake-word "$WAKE_WORD")
  if [[ "${MIND_BOX_WAKE_ENABLED:-1}" == "0" ]]; then
    wake_args=(--wake-word '')
  fi
  start_process bridge "$DEVICE_DIR" "$DEVICE_PY" -u pc_door_bridge.py \
    --port "$BRIDGE_PORT" --door-source vision --record-audio --audio-tail 0 "${wake_args[@]}"
  start_process backend "$BACKEND_DIR" "$BACKEND_PY" -m app.server --port "$BACKEND_PORT"
  start_process web "$WEB_DIR" "$WEB_NPM" run dev -- --host 127.0.0.1 --port "$WEB_PORT" --strictPort

  wait_http bridge "http://127.0.0.1:$BRIDGE_PORT/api/camera/state"
  wait_http backend "http://127.0.0.1:$BACKEND_PORT/api/voice/health"
  wait_http web "http://127.0.0.1:$WEB_PORT/"
  say "WebUI: http://127.0.0.1:$WEB_PORT/"
  say "logs: $LOG_DIR/{bridge,backend,web}.log"
  say "the VS Code CanMV window must remain connected for camera frames"
}

status() {
  local port
  for port in "$BRIDGE_PORT" "$BACKEND_PORT" "$WEB_PORT"; do
    if [[ -n "$(port_pids "$port")" ]]; then
      say "port $port: running ($(port_pids "$port" | tr '\n' ' '))"
    else
      say "port $port: stopped"
    fi
  done
}

case "${1:-restart}" in
  start)
    start_all
    ;;
  restart|reset)
    stop_all
    start_all
    ;;
  stop)
    stop_all
    say "services stopped; data was kept"
    ;;
  status)
    status
    ;;
  *)
    printf 'Usage: %s {start|restart|reset|stop|status}\n' "$0"
    exit 2
    ;;
esac
