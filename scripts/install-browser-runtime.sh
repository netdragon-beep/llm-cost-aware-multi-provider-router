#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
"$RELAYDECK_PYTHON" -m pip install -r "$RELAYDECK_ROOT/requirements.txt"
"$RELAYDECK_PYTHON" -m playwright install chromium
