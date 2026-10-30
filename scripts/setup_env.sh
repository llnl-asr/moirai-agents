#!/usr/bin/env bash
# Create .venv with moirai-agents and dftracer-agents, EDITABLE, so one environment serves the web UI, both MCP servers and the workspace.
#
#   scripts/setup_env.sh                                    # uses ../dftracer-agents, or clones develop to vendor/dftracer-agents
#   DFTRACER_AGENTS_SRC=/path/to/checkout scripts/setup_env.sh
#
# moirai itself lives in its own venv: set MOIRAI_AGENTS_MOIRAI_PYTHON in .env.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
SRC="${DFTRACER_AGENTS_SRC:-}"
[ -n "$SRC" ] || { [ -d "$REPO/../dftracer-agents/.git" ] && SRC="$REPO/../dftracer-agents" || SRC="$REPO/vendor/dftracer-agents"; }
# The site pip config can add an index that does not resolve here.
export PIP_INDEX_URL=https://pypi.org/simple PIP_EXTRA_INDEX_URL= PIP_CONFIG_FILE=/dev/null
[ -d "$REPO/.venv" ] || "$PY" -m venv "$REPO/.venv"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"
python -m pip install -q --upgrade pip setuptools wheel
[ -d "$SRC/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${DFTRACER_AGENTS_REF:-develop}" https://github.com/llnl-asr/dftracer-agents.git "$SRC"
# dftracer-agents pins its deps as git URLs (they would build C++ from source): install it without deps, and add them from PyPI wheels.
python -m pip install -q --no-deps -e "$SRC"
python -m pip install -q fastmcp "libclang>=18.1.0" matplotlib requests "graphifyy>=0.9.11" "mlflow>=2.9" "pypdf>=4.0" dftracer-utils pydftracer
python -m pip install -q -e "$REPO[dev]"
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; chmod 600 "$REPO/.env"; echo "created .env: fill it in"; }
PYTHONPATH="$REPO/src" python -m moirai_agents.workspace
python -m pip list 2>/dev/null | grep -i -E '^(dftracer|moirai|fastmcp)'
