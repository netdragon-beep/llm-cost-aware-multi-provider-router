#!/bin/sh
set -eu

RELAYDECK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RELAYDECK_ENV_ROOT=${RELAYDECK_ENV_ROOT:-"$RELAYDECK_ROOT/.venv"}
RELAYDECK_PYTHON=${RELAYDECK_PYTHON:-"$RELAYDECK_ENV_ROOT/bin/python"}
RELAYDECK_RUN_DIR="$RELAYDECK_ROOT/run"
RELAYDECK_LOG_DIR="$RELAYDECK_ROOT/logs"

if [ ! -x "$RELAYDECK_PYTHON" ]; then
  RELAYDECK_PYTHON=$(command -v python3)
fi

load_dotenv() {
  [ -f "$RELAYDECK_ROOT/.env" ] || { echo "Missing .env" >&2; exit 1; }
  set -a
  # The documented .env format is shell-compatible KEY=value lines.
  . "$RELAYDECK_ROOT/.env"
  set +a
}

ensure_runtime_dirs() {
  mkdir -p "$RELAYDECK_RUN_DIR" "$RELAYDECK_LOG_DIR"
}

pid_file() { printf '%s/%s.pid\n' "$RELAYDECK_RUN_DIR" "$1"; }

stop_pid() {
  file=$(pid_file "$1")
  if [ -f "$file" ]; then
    pid=$(cat "$file" 2>/dev/null || true)
    case "$pid" in
      *[!0-9]*|'') ;;
      *) kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true ;;
    esac
    rm -f "$file"
  fi
}

start_background() {
  service=$1
  shift
  nohup "$@" >"$RELAYDECK_LOG_DIR/$service.stdout.log" 2>"$RELAYDECK_LOG_DIR/$service.stderr.log" &
  printf '%s\n' "$!" >"$(pid_file "$service")"
}
