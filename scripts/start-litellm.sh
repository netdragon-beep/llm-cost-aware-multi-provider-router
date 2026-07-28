#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
load_dotenv
ensure_runtime_dirs

LITELLM=${RELAYDECK_LITELLM_BIN:-"$RELAYDECK_ENV_ROOT/bin/litellm"}
[ -x "$LITELLM" ] || LITELLM=$(command -v litellm)
MASTER_HEADER="Authorization: Bearer ${LITELLM_MASTER_KEY:?Missing LITELLM_MASTER_KEY}"
LITELLM_PORT=${LITELLM_PORT:-4100}
CLAUDE_LITELLM_PORT=${CLAUDE_LITELLM_PORT:-4101}
CLAUDE_LITELLM_INTERNAL_PORT=${CLAUDE_LITELLM_INTERNAL_PORT:-4102}

stop_pid litellm
stop_pid litellm-claude-internal
stop_pid claude-desktop-gateway
start_background litellm "$LITELLM" --config "$RELAYDECK_ROOT/config/litellm.yaml" --port "$LITELLM_PORT" --host 127.0.0.1
start_background litellm-claude-internal "$LITELLM" --config "$RELAYDECK_ROOT/config/litellm-claude.yaml" --port "$CLAUDE_LITELLM_INTERNAL_PORT" --host 127.0.0.1
CLAUDE_LITELLM_INTERNAL_PORT="$CLAUDE_LITELLM_INTERNAL_PORT" start_background claude-desktop-gateway "$RELAYDECK_PYTHON" -m uvicorn claude_desktop_gateway:app --app-dir "$RELAYDECK_ROOT" --host 127.0.0.1 --port "$CLAUDE_LITELLM_PORT"

for endpoint in "http://127.0.0.1:$LITELLM_PORT/models" "http://127.0.0.1:$CLAUDE_LITELLM_PORT/v1/models"; do
  attempt=0
  until curl -fsS -H "$MASTER_HEADER" "$endpoint" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || { echo "Gateway readiness check failed: $endpoint" >&2; exit 1; }
    sleep 1
  done
done
echo "RelayDeck gateways started"
