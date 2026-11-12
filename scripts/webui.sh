#!/usr/bin/env bash
# Run the web UI in the foreground. scripts/webui_start.sh is the usual way.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO/scripts/venv.sh"; PY="$MOIRAI_PY"     # this system's venv (scripts/venv.sh)
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
umask 007
exec "$PY" -m moirai_agents.webui.server "$@"
