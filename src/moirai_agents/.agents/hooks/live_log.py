#!/usr/bin/env python3
"""PreToolUse hook (Bash): also copy the command's output, as it is produced, to
$MOIRAI_AGENTS_LIVE_DIR/<tool_use_id>.log so the web UI can show it live while the command runs.

The command itself is unchanged: it runs in a `{ ...; }` group (no subshell, so `cd`/exports persist and
heredocs work) whose output goes to the log; an EXIT trap replays the log to the tool's stdout, so the
agent still gets the full output (stdout+stderr, as the Bash tool reports them) and the command's own
exit status, also when the command calls `exit`. (Process substitution + tee was racy: bash 4.4 cannot
wait for it, so short outputs were lost.) Outside the web UI (no MOIRAI_AGENTS_LIVE_DIR) the hook does nothing.
"""
import json
import os
import re
import shlex
import sys

live = os.environ.get("MOIRAI_AGENTS_LIVE_DIR")
try:
    ev = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
inp = ev.get("tool_input") or {}
cmd = inp.get("command")
tid = re.sub(r"[^A-Za-z0-9_-]", "", str(ev.get("tool_use_id") or ""))
if not live or not tid or not isinstance(cmd, str) or not cmd.strip() or inp.get("run_in_background"):
    sys.exit(0)
try:
    os.makedirs(live, exist_ok=True)
except OSError:
    sys.exit(0)
log = shlex.quote(os.path.join(live, tid + ".log"))
wrapped = (f"__rabbit_live={log}; : > \"$__rabbit_live\"; "
           f"trap 'cat \"$__rabbit_live\" 2>/dev/null' EXIT\n"
           f"{{\n{cmd}\n}} >> \"$__rabbit_live\" 2>&1")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "allow",
    "updatedInput": dict(inp, command=wrapped)}}))
