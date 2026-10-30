#!/usr/bin/env bash
# Set up the OpenAI bridge: users whose model access is OpenAI (or an OpenAI-compatible gateway) run Claude Code
# through a private LiteLLM proxy. LiteLLM gets its own venv (.venv-bridge) because its proxy extras pin packages
# the agent's .venv needs at other versions.
#
#   scripts/setup_openai_bridge.sh          # create/update .venv-bridge with litellm[proxy] and check it
#   scripts/setup_openai_bridge.sh --check  # only report whether the bridge is usable
#
# Installs from pypi.org only: an unreachable extra index in a pip config makes every lookup retry for minutes.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv-bridge"
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,9p' "$0"; exit 0; }
if [ "${1:-}" = "--check" ]; then
  [ -x "$VENV/bin/litellm" ] && { echo "✓ bridge: $VENV/bin/litellm ($("$VENV/bin/python" -c 'import importlib.metadata as m; print("litellm", m.version("litellm"))'))"; exit 0; }
  echo "✗ bridge not installed: run $0"; exit 1
fi
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" || { echo "✗ could not create $VENV" >&2; exit 1; }
echo "→ installing litellm[proxy] into $VENV (a few minutes) ..."
"$VENV/bin/python" -m pip install --isolated --index-url https://pypi.org/simple -q --upgrade "litellm[proxy]" \
  || { echo "✗ pip failed (no network to pypi.org?)" >&2; exit 1; }
[ -x "$VENV/bin/litellm" ] || { echo "✗ $VENV/bin/litellm missing after install" >&2; exit 1; }
echo "✓ bridge ready: $VENV/bin/litellm ($("$VENV/bin/python" -c 'import importlib.metadata as m; print("litellm", m.version("litellm"))'))"
echo "  Users now pick OpenAI in the web UI (⚙ Model settings); the bridge starts on first use, no restart needed."
