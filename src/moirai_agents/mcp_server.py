"""The MCP server: dependency-free stdio JSON-RPC. It has no tools of its own: every tool comes from a capability.

It refreshes itself. A capability's tool.py is re-imported when it changes. A change to this package's code re-executes
the process in place, at a moment no request is running and no input is buffered: stdin and stdout are inherited, so the
client's connection is unbroken and no restart of the server or the chat is needed, including for this file. Either way
``notifications/tools/list_changed`` tells the client. Broken edits are refused: the running code stays.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "moirai_agents"

from . import __version__                                           # noqa: E402
from .core import capstore, knowledge, selfrefresh                             # noqa: E402
from .env import group_umask, load_env                              # noqa: E402

PROTOCOL = "2024-11-05"
PKG = Path(__file__).resolve().parent
POLL = float(os.environ.get("MOIRAI_AGENTS_MCP_POLL", "2"))
TOOLS: Dict[str, Dict[str, Any]] = {}
OUT = threading.Lock()                     # one writer on stdout at a time; held across a request


def _code_files():
    return [p for p in PKG.rglob("*.py") if not ({"webui", "__pycache__", ".agents"} & set(p.parts))]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _notify_changed() -> None:
    _send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def refresh() -> bool:
    r = capstore.refresh(TOOLS)
    return bool(r.get("changed")) or capstore.take_notify()


def handle(req: dict):
    m = req.get("method")
    if m == "initialize":
        return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "moirai-agents", "version": __version__}}
    if m == "ping":
        return {}
    if m == "tools/list":
        refresh()
        return {"tools": [t["schema"] for t in TOOLS.values()]}
    if m == "tools/call":
        p = req.get("params") or {}
        refresh()
        t = TOOLS.get(p.get("name"))
        if not t:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {p.get('name')}"}]}
        try:
            res = knowledge.on_failure(p["name"], t["fn"](**(p.get("arguments") or {})))
            text = res if isinstance(res, str) else json.dumps(res, indent=1, default=str)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            known = knowledge.on_failure(p["name"], {"status": "error", "error": f"{type(e).__name__}: {e}"}).get("known_lessons")
            text = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}" + (f"\nknown_lessons: {json.dumps(known)}" if known else "")
            return {"isError": True, "content": [{"type": "text", "text": text}]}
    raise KeyError(m)


class Lines:
    """Line reader on fd 0 with no hidden buffer: the watcher must know that no input is pending before it re-executes."""

    def __init__(self) -> None:
        self.buf = b""
        self.busy = False                  # set (under OUT) before reading, so input is never lost to a re-execution

    def next(self):
        while b"\n" not in self.buf:
            select.select([0], [], [])
            with OUT:
                self.busy = True
            chunk = os.read(0, 65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line


def _watch(lines: Lines) -> None:
    """Capabilities refresh in place; a change to the kernel re-executes the process when it is idle."""
    w = selfrefresh.Watcher(_code_files)
    while True:
        time.sleep(POLL)
        changed, err = w.changes()
        if err:
            print(f"[moirai-mcp] keeping the running code: {err}", file=sys.stderr, flush=True)
        with OUT:
            if changed and not lines.busy:
                print(f"[moirai-mcp] refreshing for {len(changed)} changed file(s)", file=sys.stderr, flush=True)
                os.environ["MOIRAI_AGENTS_MCP_REFRESHED"] = "1"
                os.execv(sys.executable, [sys.executable, "-m", "moirai_agents.mcp_server"] + sys.argv[1:])
            try:
                if refresh():
                    _notify_changed()
            except Exception:
                print(traceback.format_exc(limit=3), file=sys.stderr, flush=True)


def serve() -> None:
    lines = Lines()
    threading.Thread(target=_watch, args=(lines,), daemon=True, name="self-refresh").start()
    if os.environ.pop("MOIRAI_AGENTS_MCP_REFRESHED", None):
        _notify_changed()
    while True:
        raw = lines.next()
        if raw is None:
            return
        try:
            req = json.loads(raw) if raw.strip() else {}
        except ValueError:
            req = {}
        if "id" not in req:
            lines.busy = bool(lines.buf)
            continue
        with OUT:
            try:
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": handle(req)}
            except KeyError as e:
                resp = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": f"method not found: {e}"}}
            except Exception as e:  # pragma: no cover
                resp = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": str(e)}}
            _send(resp)
            lines.busy = bool(lines.buf)


def main(argv=None) -> int:
    argparse.ArgumentParser(description="moirai-agents MCP server (stdio)").parse_args(argv)
    load_env()
    group_umask()
    refresh()
    capstore.take_notify()
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
