#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
load_dotenv
ensure_runtime_dirs
stop_pid admin-panel
start_background admin-panel "$RELAYDECK_PYTHON" -m uvicorn app:app --app-dir "$RELAYDECK_ROOT/admin-panel" --host 127.0.0.1 --port "${ADMIN_PANEL_PORT:-8091}"
echo "Admin panel started"
