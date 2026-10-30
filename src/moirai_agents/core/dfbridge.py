"""Call dftracer-agents' MCP tools in-process (the wrappers own the pipeline; dftracer owns the mechanics).

dftracer-agents is imported into this process (``build_server("both")``) and its tools are called directly, so
moirai-agents controls the server's lifecycle:

* **auto-refresh** - before every call the bridge fingerprints the dftracer_agents package (path + size + mtime of
  every .py). When it changed (an edit, a ``pip install -e`` checkout switch), every ``dftracer_agents.*`` module is
  dropped and the server is rebuilt, so the next call runs the new code. ``MOIRAI_AGENTS_DFTRACER_AUTOREFRESH=0`` turns
  that off; :func:`refresh` forces a rebuild (``df_refresh`` tool).
* **one session root** - ``DFTRACER_WORKSPACES`` = env, else ``$MOIRAI_AGENTS_USER_WORKSPACE/workspaces``. Every
  dftracer session of a pipeline lives there; use the capabilities (or ``df_call``) for pipeline work, not
  another dftracer server that has a different root.

Every call returns the tool's JSON as a dict (``{"status": "error", ...}`` on failure, never an exception).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parents[3]
_STATE: Dict[str, Any] = {"srv": None, "fp": None, "built": None, "builds": 0, "error": None}
_LOCK = threading.RLock()


def workspaces_root() -> str:
    if os.environ.get("DFTRACER_WORKSPACES"):
        return os.environ["DFTRACER_WORKSPACES"]
    ws = os.environ.get("MOIRAI_AGENTS_USER_WORKSPACE") or str(REPO / "workspace")
    return str(Path(ws) / "workspaces")


def _pkg_dir() -> Optional[Path]:
    spec = importlib.util.find_spec("dftracer_agents")
    return Path(spec.origin).parent if spec and spec.origin else None


def available() -> bool:
    """dftracer-agents and its MCP stack are installed (scripts/setup_env.sh, editable)."""
    return _pkg_dir() is not None and importlib.util.find_spec("fastmcp") is not None


def fingerprint() -> str:
    d = _pkg_dir()
    if not d:
        return "missing"
    h = hashlib.sha1()
    for p in sorted(d.rglob("*.py")):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.relative_to(d)}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _drop_modules() -> None:
    for name in [m for m in sys.modules if m == "dftracer_agents" or m.startswith("dftracer_agents.")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _build(reason: str) -> None:
    os.environ["DFTRACER_WORKSPACES"] = workspaces_root()
    Path(os.environ["DFTRACER_WORKSPACES"]).mkdir(parents=True, exist_ok=True)
    if _STATE["srv"] is not None:
        _drop_modules()
    t0 = time.time()
    from dftracer_agents.mcp_server import build_server
    _STATE.update(srv=build_server("both"), fp=fingerprint(), built=round(time.time(), 1),
                  build_s=round(time.time() - t0, 1), reason=reason, error=None)
    _STATE["builds"] += 1


def server(force: bool = False):
    with _LOCK:
        if _STATE["srv"] is None or force:
            _build("forced" if force else "first use")
        elif os.environ.get("MOIRAI_AGENTS_DFTRACER_AUTOREFRESH", "1") != "0":
            fp = fingerprint()
            if fp != _STATE["fp"]:
                _build(f"dftracer_agents code changed ({_STATE['fp']} -> {fp})")
        return _STATE["srv"]


def refresh() -> Dict[str, Any]:
    """Force a rebuild now (drops every dftracer_agents module first)."""
    try:
        server(force=True)
    except Exception as e:
        _STATE["error"] = f"{type(e).__name__}: {e}"
    return status()


def status() -> Dict[str, Any]:
    return {"status": "error" if _STATE["error"] else "ok", "loaded": _STATE["srv"] is not None,
            "fingerprint": _STATE["fp"], "current_fingerprint": fingerprint(), "builds": _STATE["builds"],
            "built": _STATE["built"], "build_s": _STATE.get("build_s"), "last_reason": _STATE.get("reason"),
            "autorefresh": os.environ.get("MOIRAI_AGENTS_DFTRACER_AUTOREFRESH", "1") != "0",
            "workspaces": workspaces_root(), "package": str(_pkg_dir()), "error": _STATE["error"]}


def _text_of(content) -> str:
    parts = []
    for c in content or []:
        t = c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


def _as_dict(text: str) -> Dict[str, Any]:
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {"status": "ok", "result": d}
    except ValueError:
        return {"status": "ok", "text": text}


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    out: Dict[str, Any] = {}                          # called from inside a loop: run on a private thread
    th = threading.Thread(target=lambda: out.setdefault("r", asyncio.run(coro)))
    th.start()
    th.join()
    return out.get("r")


def call(tool: str, args: Optional[dict] = None) -> Dict[str, Any]:
    """Call one dftracer-agents tool by name with keyword arguments; returns its JSON as a dict."""
    args = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        srv = server()
        res = _run(srv.call_tool(tool, args))
    except Exception as e:                            # surface, never raise into the MCP server
        return {"status": "error", "error": f"dftracer {tool}: {type(e).__name__}: {e}"}
    content = getattr(res, "content", None)
    if content is None and isinstance(res, (list, tuple)):
        content = res
    d = _as_dict(_text_of(content))
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        d = {"status": "error", "error": d.get("error") or d.get("text") or json.dumps(d)[:2000]}
    return d


def tools() -> list:
    srv = server()
    return sorted(t.name for t in _run(srv.list_tools()))


def ok(d: Dict[str, Any]) -> bool:
    return str(d.get("status", "ok")).lower() in ("ok", "success", "completed", "done") and not d.get("error")
