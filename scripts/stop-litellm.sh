#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
ensure_runtime_dirs
stop_pid claude-desktop-gateway
stop_pid litellm-claude-internal
stop_pid litellm
echo "RelayDeck gateways stopped"
