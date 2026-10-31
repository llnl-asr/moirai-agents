#!/usr/bin/env bash
# Stop the web UI on this node and everything it started (chats, MCP servers, jobs). --status only shows them.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cmd=stop; [ "${1:-}" = "--status" ] && cmd=status
cd "$REPO" && PYTHONPATH="$REPO/src" "$PY" -m moirai_agents.webui.procs "$cmd"
