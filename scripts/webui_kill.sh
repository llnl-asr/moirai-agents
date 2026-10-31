#!/usr/bin/env bash
# Stop moirai-agents web UI servers (scripts/webui.sh) left running on any login node.
#
#   scripts/webui_kill.sh             # stop every server of $USER on all login nodes
#   scripts/webui_kill.sh --list      # only show what is running
#   scripts/webui_kill.sh --nodes tuolumne1001,tuolumne2151
#
# Login nodes come from `nodeattr -c login` (override with --nodes or MOIRAI_AGENTS_LOGIN_NODES).
# Only processes owned by $USER are touched: the server and its direct children (claude turns, dfdashboard
# and MCP servers get SIGTERM with it). SIGKILL after a 5 s grace period.
set -uo pipefail

list_only=0
nodes="${MOIRAI_AGENTS_LOGIN_NODES:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --list|-n|--dry-run) list_only=1 ;;
    --nodes) nodes="$2"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$nodes" ] || nodes="$(nodeattr -c login 2>/dev/null)"
[ -n "$nodes" ] || nodes="$(hostname -s)"

# Runs on each node. The [r] keeps the pattern from matching this script's own command line.
remote=$(cat <<'EOF'
pat='[m]oirai_agents[.]webui[.]server'
pids=$(pgrep -u "$(id -un)" -f "$pat")
[ -n "$pids" ] || { echo "  none"; exit 0; }
for p in $pids; do
  echo "  pid $p: $(ps -o etime=,args= -p "$p" | sed 's/^ *//' | cut -c1-150)"
done
[ "$LIST_ONLY" = 1 ] && exit 0
kids=$(for p in $pids; do pgrep -P "$p"; done)
kill -TERM $pids $kids 2>/dev/null
for _ in 1 2 3 4 5; do
  pgrep -u "$(id -un)" -f "$pat" >/dev/null || break
  sleep 1
done
left=$(pgrep -u "$(id -un)" -f "$pat")
[ -n "$left" ] && kill -KILL $left 2>/dev/null
echo "  stopped"
EOF
)

me="$(hostname -s)"
for h in ${nodes//,/ }; do
  echo "$h:"
  if [ "$h" = "$me" ]; then
    LIST_ONLY=$list_only bash -c "$remote"
  else
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" "LIST_ONLY=$list_only bash -s" <<<"$remote" \
      || echo "  (ssh to $h failed)"
  fi
done
