#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
load_dotenv
ensure_runtime_dirs
OPEN_WEBUI=${RELAYDECK_OPEN_WEBUI_BIN:-"$RELAYDECK_ENV_ROOT/bin/open-webui"}
[ -x "$OPEN_WEBUI" ] || OPEN_WEBUI=$(command -v open-webui)
stop_pid open-webui
start_background open-webui "$OPEN_WEBUI" serve --port "${OPEN_WEBUI_PORT:-8090}"
echo "Open WebUI started"
