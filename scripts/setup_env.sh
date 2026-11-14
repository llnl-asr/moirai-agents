#!/usr/bin/env bash
# Create this system's venv with moirai-agents, dftracer-agents and dfdashboard, the last two EDITABLE, so one environment serves the web UI, both MCP
# servers, the Dashboard and the workspace. dfdashboard's browser app is built here; Node comes from a wheel, so no system Node is needed.
#
#   scripts/setup_env.sh                                    # clones dftracer-agents (develop) and phronix (master) to vendor/;
#                                                           # dfdashboard from ../dfdashboard or vendor/
#   DFTRACER_AGENTS_SRC=/path/to/checkout PHRONIX_SRC=/path/to/checkout DFDASHBOARD_SRC=/path/to/checkout scripts/setup_env.sh
#   DFTRACER_AGENTS_REF / PHRONIX_REF                       # clone another branch
#
# moirai-agents builds on dftracer-agents and phronix (the capability engine and the general capabilities), each from a
# clone in this project's vendor/. What moirai-agents learns about one of their capabilities is committed on a
# contrib/moirai-agents branch in a worktree of that clone (vendor/contrib/<project>); the web UI's Update button
# rebases it and says how to push it and open the pull request.
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
[ -n "$SRC" ] || SRC="$REPO/vendor/dftracer-agents"
PHRONIX_SRC="${PHRONIX_SRC:-$REPO/vendor/phronix}"
# The site pip config can add an index that does not resolve here.
export PIP_INDEX_URL=https://pypi.org/simple PIP_EXTRA_INDEX_URL= PIP_CONFIG_FILE=/dev/null
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip setuptools wheel
[ -d "$SRC/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${DFTRACER_AGENTS_REF:-develop}" https://github.com/llnl-asr/dftracer-agents.git "$SRC"
# dftracer-agents pins some deps as git URLs (they would build C++ from source): install it without deps, then its
# runtime deps read from its own pyproject.toml (so its version pins, e.g. fastmcp<4, always apply here), each git
# URL replaced by the same project's PyPI wheel. dfdiagnoser has no PyPI release and stays out.
[ -d "$PHRONIX_SRC/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${PHRONIX_REF:-master}" https://github.com/llnl-asr/phronix.git "$PHRONIX_SRC"
python -m pip install -q -e "$PHRONIX_SRC"
python -m pip install -q --no-deps -e "$SRC"
python -c 'import tomllib' 2>/dev/null || python -m pip install -q tomli
mapfile -t AGENT_DEPS < <(python - "$SRC/pyproject.toml" <<'EOF'
import re, sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as fh:
    deps = tomllib.load(fh)["project"]["dependencies"]
wheels = {"dftracer-utils": "dftracer-utils", "dftracer-analyzer": "dftracer-analyzer"}
for d in deps:
    name = re.split(r"[\s<>=!~;\[@]", d, 1)[0]
    if "@" in d and "git+" in d:
        if name in wheels:
            print(wheels[name])
    else:
        print(d)
EOF
)
python -m pip install -q "${AGENT_DEPS[@]}" pydftracer
DASH="${DFDASHBOARD_SRC:-}"
[ -n "$DASH" ] || { [ -d "$REPO/../dfdashboard/.git" ] && DASH="$REPO/../dfdashboard" || DASH="$REPO/vendor/dfdashboard"; }
[ -d "$DASH/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --branch "${DFDASHBOARD_REF:-develop}" https://github.com/llnl-asr/dfdashboard.git "$DASH"
python -m pip install -q nodejs-wheel
python -m pip install -q -e "$DASH"
(cd "$DASH/web" && npm ci --no-audit --no-fund && npm run build)
echo "MOIRAI_AGENTS_DFDASHBOARD_DIST=$DASH/web/dist"   # npm ci rebuilds node_modules for this system
python -m pip install -q -e "$REPO[dev]"
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; chmod 600 "$REPO/.env"; echo "created .env: fill it in"; }
[ -f "$MOIRAI_SYSTEM_ENV" ] || { printf '# Settings for %s only, read before .env (e.g. MOIRAI_AGENTS_MOIRAI_PYTHON, MOIRAI_AGENTS_CLAUDE_BIN, MOIRAI_AGENTS_VENV).\n' "$MOIRAI_SYSTEM" > "$MOIRAI_SYSTEM_ENV"; chmod 600 "$MOIRAI_SYSTEM_ENV"; echo "created $MOIRAI_SYSTEM_ENV for settings of $MOIRAI_SYSTEM only"; }
PYTHONPATH="$REPO/src" python -m moirai_agents.workspace
python -m pip list 2>/dev/null | grep -i -E '^(dftracer|moirai|fastmcp|phronix)'
