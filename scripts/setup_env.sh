#!/usr/bin/env bash
# Create this system's venv with moirai-agents, dftracer-agents and dfdashboard, the last two EDITABLE, so one environment serves the web UI, both MCP
# servers, the Dashboard and the workspace. dfdashboard's browser app is built here; Node comes from a wheel, so no system Node is needed.
#
#   scripts/setup_env.sh                                    # uses ../dftracer-agents and ../dfdashboard, or clones develop to vendor/
#   DFTRACER_AGENTS_SRC=/path/to/checkout DFDASHBOARD_SRC=/path/to/checkout scripts/setup_env.sh
#   MOIRAI_AGENTS_VENV=/path/to/venv scripts/setup_env.sh     # put the venv elsewhere (also settable in .env.<system>)
#
# Run it once on every system that uses this checkout. The system is the hostname without its node number (mammoth34 -> mammoth);
# its venv is <repo>/.venv-<system> unless MOIRAI_AGENTS_VENV says otherwise, so systems sharing the file system never share a venv.
# scripts/venv.sh prints what this system resolves to.
#
# moirai itself lives in its own venv: set MOIRAI_AGENTS_MOIRAI_PYTHON in .env.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=venv.sh
source "$REPO/scripts/venv.sh"
VENV="$MOIRAI_VENV"
echo "system $MOIRAI_SYSTEM: venv $VENV"
PY="${PYTHON:-python3}"
SRC="${DFTRACER_AGENTS_SRC:-}"
[ -n "$SRC" ] || { [ -d "$REPO/../dftracer-agents/.git" ] && SRC="$REPO/../dftracer-agents" || SRC="$REPO/vendor/dftracer-agents"; }
# The site pip config can add an index that does not resolve here.
export PIP_INDEX_URL=https://pypi.org/simple PIP_EXTRA_INDEX_URL= PIP_CONFIG_FILE=/dev/null
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip setuptools wheel
[ -d "$SRC/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${DFTRACER_AGENTS_REF:-develop}" https://github.com/llnl-asr/dftracer-agents.git "$SRC"
# dftracer-agents pins its deps as git URLs (they would build C++ from source): install it without deps, and add them from PyPI wheels.
python -m pip install -q --no-deps -e "$SRC"
python -m pip install -q fastmcp httpx pyyaml "libclang>=18.1.0" matplotlib requests "graphifyy>=0.9.11" "mlflow>=2.9" "pypdf>=4.0" dftracer-utils pydftracer
DASH="${DFDASHBOARD_SRC:-}"
[ -n "$DASH" ] || { [ -d "$REPO/../dfdashboard/.git" ] && DASH="$REPO/../dfdashboard" || DASH="$REPO/vendor/dfdashboard"; }
[ -d "$DASH/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${DFDASHBOARD_REF:-develop}" https://github.com/llnl-asr/dfdashboard.git "$DASH"
python -m pip install -q nodejs-wheel
python -m pip install -q -e "$DASH"
(cd "$DASH/web" && npm ci --no-audit --no-fund && npm run build)
echo "MOIRAI_AGENTS_DFDASHBOARD_DIST=$DASH/web/dist"   # npm ci rebuilds node_modules for this system
python -m pip install -q -e "$REPO[dev]"
# The group's shared capability store (agent-capabilities): its package, then this project's capabilities linked
# into it -- each entry of .agents/capabilities becomes a link to capabilities/<topic>/<name> there, shared with the
# other agent projects, and a capability made here is moved into it. umask 007 keeps what is written group-usable.
CAPS="${AGENT_CAPABILITIES_ROOT:-/p/vast1/dftracer-user/agent-capabilities}"
if [ -d "$CAPS/agent_capabilities" ]; then
  python -m pip install -q -e "$CAPS"
  (umask 007; python -m agent_capabilities link "$REPO/src/moirai_agents/.agents/capabilities" --project moirai-agents >/dev/null) \
    && echo "capabilities linked to $CAPS" || echo "WARNING: could not link capabilities to $CAPS"
fi
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; chmod 600 "$REPO/.env"; echo "created .env: fill it in"; }
[ -f "$MOIRAI_SYSTEM_ENV" ] || { printf '# Settings for %s only, read before .env (e.g. MOIRAI_AGENTS_MOIRAI_PYTHON, MOIRAI_AGENTS_CLAUDE_BIN, MOIRAI_AGENTS_VENV).\n' "$MOIRAI_SYSTEM" > "$MOIRAI_SYSTEM_ENV"; chmod 600 "$MOIRAI_SYSTEM_ENV"; echo "created $MOIRAI_SYSTEM_ENV for settings of $MOIRAI_SYSTEM only"; }
PYTHONPATH="$REPO/src" python -m moirai_agents.workspace
python -m pip list 2>/dev/null | grep -i -E '^(dftracer|moirai|fastmcp)'
