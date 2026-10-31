#!/usr/bin/env bash
# Set up Claude Code for the moirai-agents web UI: every chat runs `claude` as the account that runs the server.
#
#   scripts/setup_claude.sh                 # find claude, install it if missing (asks first), record it, check it
#   scripts/setup_claude.sh --yes           # install without asking (the web UI's "Install it for me" runs this)
#   scripts/setup_claude.sh --path /p/claude   # use this binary
#   scripts/setup_claude.sh --check         # only report what the server would use; change nothing
#
# Records MOIRAI_AGENTS_CLAUDE_BIN=<full path> in moirai-agents/.env (mode 600). The web UI reads .env fresh: no restart.
# The model behind it (LivAI, Anthropic, OpenAI) is chosen per user in the web UI (⚙ Model settings);
# for OpenAI also run scripts/setup_openai_bridge.sh once.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVF="${MOIRAI_AGENTS_LIVAI_ENV:-$REPO/.env}"
yes=0 check=0 path=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) yes=1 ;;
    --check) check=1 ;;
    --path) path="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

works() { [ -x "$1" ] && [ -f "$1" ] && "$1" --version 2>/dev/null | grep -qi claude; }

find_claude() {
  local configured=""
  [ -f "$ENVF" ] && configured=$(sed -n 's/^\s*\(export \)\?MOIRAI_AGENTS_CLAUDE_BIN=//p' "$ENVF" | tail -1 | tr -d "\"'")
  for c in "$configured" "${MOIRAI_AGENTS_CLAUDE_BIN:-}" "$(command -v claude 2>/dev/null)" "$HOME/.local/bin/claude" \
           "$HOME/.claude/local/claude" "$HOME/.npm-global/bin/claude" /usr/local/bin/claude; do
    [ -n "$c" ] && works "$c" && { echo "$c"; return 0; }
  done
  return 1
}

if [ -n "$path" ]; then
  path="$(cd "$(dirname "$path")" 2>/dev/null && pwd)/$(basename "$path")"
  works "$path" || { echo "✗ $path is not a working Claude Code binary ($path --version failed)" >&2; exit 1; }
  found="$path"
else
  found="$(find_claude)" || found=""
fi

if [ -z "$found" ]; then
  echo "Claude Code (claude) is not installed for $(whoami) on $(hostname -s)."
  [ "$check" = 1 ] && exit 1
  if [ "$yes" != 1 ]; then
    read -r -p "Install it now with the official installer (curl -fsSL https://claude.ai/install.sh | bash)? [y/N] " a
    [[ "$a" =~ ^[Yy] ]] || { echo "Nothing installed. Install it yourself, then: $0 --path \$(command -v claude)"; exit 1; }
  fi
  echo "→ installing Claude Code into ~/.local/bin ..."
  if ! curl -fsSL https://claude.ai/install.sh | bash; then
    if command -v npm >/dev/null; then
      echo "→ the installer failed; trying npm install -g @anthropic-ai/claude-code"
      npm install -g @anthropic-ai/claude-code || true
    fi
  fi
  found="$(find_claude)" || { echo "✗ installation did not produce a working claude (no network to claude.ai? see above)" >&2; exit 1; }
fi

ver="$("$found" --version 2>/dev/null | head -1)"
echo "✓ Claude Code: $found ($ver)"
[ "$check" = 1 ] && exit 0

# record it for the web UI (replace an existing line, keep the file private)
touch "$ENVF" && chmod 600 "$ENVF"
tmp="$(mktemp "$ENVF.XXXX")"
grep -v -E '^\s*(export )?MOIRAI_AGENTS_CLAUDE_BIN=' "$ENVF" > "$tmp"
echo "MOIRAI_AGENTS_CLAUDE_BIN=$found" >> "$tmp"
chmod 600 "$tmp" && mv "$tmp" "$ENVF"
echo "✓ recorded MOIRAI_AGENTS_CLAUDE_BIN=$found in $ENVF (the web UI picks it up without a restart)"
echo
echo "Next:"
echo "  • users choose their model in the web UI (⚙ Model settings: LivAI, Anthropic or OpenAI)"
echo "  • the owner may use this account's own Claude login: run '$found' once and /login"
echo "  • OpenAI keys need the bridge once: scripts/setup_openai_bridge.sh"
