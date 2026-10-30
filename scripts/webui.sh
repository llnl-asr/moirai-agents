#!/usr/bin/env bash
# Run the web UI in the foreground. scripts/webui_start.sh is the usual way.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
umask 007
exec "$PY" -m moirai_agents.webui.server "$@"
