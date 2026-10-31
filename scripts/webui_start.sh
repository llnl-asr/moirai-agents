#!/usr/bin/env bash
# Start the web UI in the background with a log file. It includes the chats, the pipelines and the Dashboard: there is nothing else to start.
#
#   scripts/webui_start.sh                 # free port from 8766 up; prints your link, tunnel and log path
#   scripts/webui_start.sh --port 9000
#   scripts/webui_start.sh --restart       # stop the running server on this node first
#
# It stops by itself: after MOIRAI_AGENTS_WEBUI_IDLE_S (2 h) idle, at most after MOIRAI_AGENTS_WEBUI_MAX_S (12 h).
# Code changes apply by themselves; a restart is never needed for them.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$(hostname -s)"
RUN="$REPO/logs/webui"
mkdir -p "$RUN" && chmod 700 "$REPO/logs" "$RUN" 2>/dev/null
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
procs() { (cd "$REPO" && PYTHONPATH="$REPO/src" "$PY" -m moirai_agents.webui.procs "$@"); }
restart=0; args=()
for a in "$@"; do case "$a" in --restart) restart=1 ;; -h|--help) sed -n '2,9p' "$0"; exit 0 ;; *) args+=("$a") ;; esac; done
running=$(procs status --json | python3 -c 'import json,sys; print(" ".join(str(s["pid"]) for s in json.load(sys.stdin)["servers"] if s["alive"]))')
if [ -n "$running" ]; then
  if [ "$restart" = 1 ]; then "$REPO/scripts/webui_stop.sh"
  else echo "a web UI server is already running on $HOST (pid $running); use --restart or scripts/webui_stop.sh"; exit 1; fi
fi
LOG="$RUN/webui-$HOST-$(date +%Y%m%d-%H%M%S).log"
( umask 077; : > "$LOG" )
ln -sfn "$(basename "$LOG")" "$RUN/latest-$HOST.log"
MOIRAI_AGENTS_WEBUI_LOG="$LOG" PYTHONUNBUFFERED=1 nohup setsid "$REPO/scripts/webui.sh" "${args[@]}" >> "$LOG" 2>&1 < /dev/null &
spid=$!
for _ in $(seq 1 120); do grep -q "stops after" "$LOG" && break; kill -0 "$spid" 2>/dev/null || break; sleep 0.5; done
grep -q "stops after" "$LOG" || { echo "the web UI did not start; last lines of $LOG:" >&2; tail -20 "$LOG" >&2; exit 1; }
grep -E "web UI:|tunnel:|stops after" "$LOG"
echo "  log:  $RUN/latest-$HOST.log"
echo "  stop: scripts/webui_stop.sh"
