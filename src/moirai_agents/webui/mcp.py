"""MCP servers of the workspace: their state, a check, and a restart (Settings panel).

check: start a server the way Claude Code does (stdio: initialize, then tools/list) and say whether it works - seconds
to answer, number of tools, or why it failed (the exception line of a crash, a time-out, a missing program).
restart: stop the copies of a server serving this web UI's chats, then check it. Every chat message starts fresh copies
anyway; a chat running right now loses the server until its next message.

    python -m moirai_agents.webui.mcp [workspace]      # check by hand; exit 1 if any server fails
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List

from . import procs

CHECKS: Dict[str, dict] = {}            # server name -> last check


def _crash_reason(stderr: str) -> str:
    tail = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not tail:
        return "exited without output"
    exc = next((ln for ln in reversed(tail) if re.match(r"^[A-Za-z_.]+(Error|Exception|Exit)\b", ln)), tail[-1])
    where = next((ln for ln in reversed(tail) if ln.startswith("File ")), "")
    return exc + (f" ({where})" if where else "")


def check_server(name: str, spec: dict, cwd: str, timeout: float = 120.0) -> dict:
    if spec.get("type") in ("http", "sse") or "url" in spec:
        return {"name": name, "ok": None, "detail": f"remote ({spec.get('url')}): not started here"}
    cmd = [spec.get("command", "")] + list(spec.get("args") or [])
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             cwd=cwd, env={**os.environ, **(spec.get("env") or {})}, start_new_session=True)
    except OSError as e:
        return {"name": name, "ok": False, "detail": f"cannot start {cmd[0]}: {e}"}
    err: List[str] = []
    threading.Thread(target=lambda: err.extend(p.stderr), daemon=True).start()     # a chatty stderr must not block it

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def recv(want_id, deadline):
        while time.time() < deadline:
            if p.poll() is not None and not select.select([p.stdout], [], [], 0)[0]:
                return None
            if select.select([p.stdout], [], [], 0.5)[0]:
                line = p.stdout.readline()
                if not line:
                    return None
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("id") == want_id:
                    return m
        return "timeout"
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcpcheck", "version": "1"}}})
        r = recv(1, t0 + timeout)
        if isinstance(r, dict) and "result" in r:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            r2 = recv(2, time.time() + 60)
            n = len(r2["result"].get("tools") or []) if isinstance(r2, dict) and "result" in r2 else None
            return {"name": name, "ok": n is not None, "secs": round(time.time() - t0, 1), "tools": n,
                    "detail": f"{n} tools in {time.time() - t0:.1f} s" if n is not None else "answered initialize but not tools/list"}
        if r == "timeout":
            return {"name": name, "ok": False, "secs": timeout, "detail": f"no answer within {timeout:.0f} s"}
        p.wait(timeout=5)
        time.sleep(0.2)
        return {"name": name, "ok": False, "detail": "crashed at start: " + _crash_reason("".join(err)), "stderr": "".join(err)[-3000:]}
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        return {"name": name, "ok": False, "detail": "crashed at start: " + _crash_reason("".join(err))}
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                p.kill()


def specs(ws: str) -> dict:
    try:
        return json.loads((Path(ws) / ".mcp.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        return {}


def copies(spec: dict) -> List[int]:
    """This web server's running copies of an MCP server (its children, matched by command line)."""
    want = " ".join([spec.get("command", "")] + list(spec.get("args") or []))
    inst = os.environ.get(procs.TAG)
    return [pid for pid in procs.tagged(inst) if want and want in procs._cmd(pid)]


def _parallel(fn, items):
    out = [None] * len(items)

    def run(i):
        try:
            out[i] = fn(items[i])
        except Exception as e:  # noqa: BLE001
            out[i] = {"name": items[i], "ok": False, "detail": f"{type(e).__name__}: {e}"}
    ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(items))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def state(ws: str, chats_running: int = 0) -> dict:
    rows = [{"name": n, "command": Path(s.get("command", "")).name, "processes": len(copies(s)), "check": CHECKS.get(n)}
            for n, s in specs(ws).items()]
    return {"servers": rows, "chats_running": chats_running}


def check(ws: str, names: List[str]) -> dict:
    sp = specs(ws)
    names = [n for n in names if n in sp]
    res = _parallel(lambda n: check_server(n, sp[n], ws), names)
    for n, r in zip(names, res):
        CHECKS[n] = {**r, "at": time.time()}
    return {n: CHECKS[n] for n in names}


def restart(ws: str, name: str) -> dict:
    sp = specs(ws)
    names = list(sp) if name == "all" else [name]
    if not names or any(n not in sp for n in names):
        return {"error": f"unknown MCP server {name!r}; known: {sorted(sp)}"}
    stopped = {n: procs.terminate(copies(sp[n]), grace=3.0) for n in names}
    return {"status": "ok", "stopped": stopped, "checks": check(ws, names)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        ws = argv[0]
    else:
        from .runtime import WS
        ws = str(WS)
    bad = 0
    for r in check(ws, list(specs(ws))).values():
        mark = "✓" if r["ok"] else "–" if r["ok"] is None else "✗"
        bad += r["ok"] is False
        print(f"{mark} {r['name']}: {r['detail']}")
        if r.get("stderr") and not r["ok"]:
            print("  " + "\n  ".join(r["stderr"].strip().splitlines()[-12:]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
