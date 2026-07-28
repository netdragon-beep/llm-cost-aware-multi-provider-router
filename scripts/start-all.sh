#!/bin/sh
set -eu
"$(dirname "$0")/start-litellm.sh"
"$(dirname "$0")/start-open-webui.sh"
"$(dirname "$0")/start-admin-panel.sh"
echo "RelayDeck Local started"
