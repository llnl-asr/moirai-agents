#!/usr/bin/env bash
# This system's venv. One checkout serves several systems (a shared file system): each system has its own venv and may have
# its own settings file. Same rules as moirai_agents/env.py:
#
#   system    MOIRAI_AGENTS_SYSTEM, else the short hostname without its node number (mammoth34 -> mammoth, tuolumne1009 -> tuolumne)
#   venv      MOIRAI_AGENTS_VENV (environment, .env.<system>, then .env), else <repo>/.venv-<system>
#   settings  .env.<system> is read before .env
#
#   source scripts/venv.sh                 # sets MOIRAI_SYSTEM, MOIRAI_VENV, MOIRAI_PY (the venv python, or python3 before setup)
#   scripts/venv.sh                        # prints them
#   scripts/venv.sh python -m mod ...      # runs a program from this system's venv (the workspace's MCP servers and hooks do this)
_mv_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_mv_envf="${MOIRAI_AGENTS_ENV_FILE:-$_mv_repo/.env}"

_mv_value() {   # _mv_value FILE NAME: the last NAME=value in FILE, unquoted
  [ -f "$1" ] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$2=//p" "$1" | tail -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//"
}

MOIRAI_SYSTEM="${MOIRAI_AGENTS_SYSTEM:-}"
if [ -z "$MOIRAI_SYSTEM" ]; then
  _mv_host="$(hostname -s 2>/dev/null || hostname)"
  MOIRAI_SYSTEM="$(printf '%s' "$_mv_host" | sed -E 's/[-_]?[0-9]+$//')"
  [ -n "$MOIRAI_SYSTEM" ] || MOIRAI_SYSTEM="$_mv_host"
fi
MOIRAI_SYSTEM="$(printf '%s' "$MOIRAI_SYSTEM" | tr -c 'A-Za-z0-9_.\n-' '_')"
MOIRAI_SYSTEM_ENV="$_mv_envf.$MOIRAI_SYSTEM"

MOIRAI_VENV="${MOIRAI_AGENTS_VENV:-}"
[ -n "$MOIRAI_VENV" ] || MOIRAI_VENV="$(_mv_value "$MOIRAI_SYSTEM_ENV" MOIRAI_AGENTS_VENV)"
[ -n "$MOIRAI_VENV" ] || MOIRAI_VENV="$(_mv_value "$_mv_envf" MOIRAI_AGENTS_VENV)"
[ -n "$MOIRAI_VENV" ] || MOIRAI_VENV="$_mv_repo/.venv-$MOIRAI_SYSTEM"
MOIRAI_VENV="${MOIRAI_VENV/#\~/$HOME}"
MOIRAI_PY="$MOIRAI_VENV/bin/python"; [ -x "$MOIRAI_PY" ] || MOIRAI_PY="$(command -v python3)"
export MOIRAI_AGENTS_SYSTEM="$MOIRAI_SYSTEM"

if [ "${BASH_SOURCE[0]}" = "$0" ]; then      # executed, not sourced
  if [ $# -eq 0 ]; then
    echo "system:   $MOIRAI_SYSTEM"
    echo "venv:     $MOIRAI_VENV$([ -x "$MOIRAI_VENV/bin/python" ] || echo '   (not built: scripts/setup_env.sh)')"
    echo "settings: $MOIRAI_SYSTEM_ENV$([ -f "$MOIRAI_SYSTEM_ENV" ] || echo '   (none)'), then $_mv_envf"
    exit 0
  fi
  [ -x "$MOIRAI_VENV/bin/python" ] || { echo "moirai-agents: no venv for system '$MOIRAI_SYSTEM' at $MOIRAI_VENV; run scripts/setup_env.sh on $MOIRAI_SYSTEM" >&2; exit 127; }
  prog="$1"; shift
  export VIRTUAL_ENV="$MOIRAI_VENV" PATH="$MOIRAI_VENV/bin:$PATH"
  export PYTHONPATH="$_mv_repo/src${PYTHONPATH:+:$PYTHONPATH}"
  [ -x "$MOIRAI_VENV/bin/$prog" ] && exec "$MOIRAI_VENV/bin/$prog" "$@"
  exec "$prog" "$@"
fi
unset _mv_host
