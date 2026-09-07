#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
ensure_runtime_dirs
stop_pid admin-panel
"$(dirname "$0")/stop-litellm.sh"
echo "RelayDeck Local stopped"
