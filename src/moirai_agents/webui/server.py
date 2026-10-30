"""The moirai-agents web UI: chats, the pipelines' steps, and the live Dashboard, in ONE process on one port.

    scripts/webui_start.sh                 # background, with a log; prints the URL
    scripts/webui_stop.sh                  # stop it and everything it started

It refreshes itself. api.py, prompts.py, core/ and the static files reload in place, even during a chat, and open pages pick the
change up. runtime.py, dashboard.py and this file hold running chats, so the server re-executes itself on the same listening socket as soon
as no chat is running. A change that does not compile is refused and reported on the page.

It does not outlive its use (login nodes are shared). After MOIRAI_AGENTS_WEBUI_IDLE_S (2 h) with no user action, running chat or
running job, and in any case after MOIRAI_AGENTS_WEBUI_MAX_S (12 h), it stops its chats, sweeps every process it started and exits. 0 turns a limit off.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api, dashboard, prompts, runtime as R
from ..core import capstore, jobs, learning, moirai_env, pipeline, provtrace, selfrefresh
from .. import workspace

PKG = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent
HOT = [moirai_env, pipeline, jobs, provtrace, capstore, learning, workspace, prompts, api]    # in dependency order
COLD = [WEB / f for f in ("server.py", "runtime.py", "dashboard.py", "accounts.py", "bridge.py", "procs.py")] + [PKG / "env.py", PKG / "core" / "selfrefresh.py"]
TICK = 2.0
JOB_CHECK = 60.0
WARN_S = 900


def limit(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


class Life:
    """What the page needs to know about this server: code version, refresh state, when it will stop."""

    def __init__(self, app: "R.App") -> None:
        self.app = app
        self.started = float(os.environ.get("MOIRAI_AGENTS_WEBUI_STARTED") or time.time())
        self.active = float(os.environ.get("MOIRAI_AGENTS_WEBUI_ACTIVE") or time.time())
        self.idle_s, self.max_s = limit("MOIRAI_AGENTS_WEBUI_IDLE_S", 7200), limit("MOIRAI_AGENTS_WEBUI_MAX_S", 43200)
        self.pending = False                       # long-lived code changed: waiting for the chats to end
        self.error = ""
        self.version = 0
        self.lock = threading.Lock()
        self.reason = ""

    def touch(self) -> None:
        self.active = time.time()

    def stops_in(self) -> dict:
        now = time.time()
        left = [("idle", self.active + self.idle_s - now)] if self.idle_s else []
        if self.max_s:
            left.append(("maximum lifetime", self.started + self.max_s - now))
        if not left:
            return {"seconds": None, "why": ""}
        why, s = min(left, key=lambda x: x[1])
        return {"seconds": int(s), "why": why, "warn": s < WARN_S}

    def snapshot(self) -> dict:
        return {"version": self.version, "pending": self.pending, "error": self.error, "stops": self.stops_in()}


def static_stamp() -> str:
    h = hashlib.sha1()
    for p in sorted(R.STATIC.iterdir()):
        h.update(f"{p.name}:{p.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:12]


def reload_hot(changed: list, life: Life) -> None:
    """Reload changed hot modules in dependency order, and everything after the first that changed."""
    names = [m.__file__ for m in HOT]
    first = min((names.index(c) for c in changed if c in names), default=None)
    if first is None:
        return
    try:
        for m in HOT[first:]:
            importlib.reload(m)
        life.error = ""
        print(f"{time.strftime('%F %T')} refreshed: {', '.join(Path(c).name for c in changed)}", flush=True)
    except Exception as e:                         # the modules that loaded stay; the old api keeps serving
        life.error = f"{type(e).__name__}: {e}"
        print(traceback.format_exc(limit=3), flush=True)


def reexec(app: "R.App", life: Life, srv) -> None:
    """Replace this process's code, keeping the socket: the browser sees at most a brief pause."""
    print(f"{time.strftime('%F %T')} re-executing for the new code", flush=True)
    fd = srv.socket.fileno()
    os.set_inheritable(fd, True)
    os.environ.update(MOIRAI_AGENTS_WEBUI_FD=str(fd), MOIRAI_AGENTS_WEBUI_STARTED=str(life.started), MOIRAI_AGENTS_WEBUI_ACTIVE=str(life.active))
    os.execv(sys.executable, [sys.executable, "-m", "moirai_agents.webui.server"] + sys.argv[1:])


def jobs_running(app: "R.App") -> bool:
    for ws in list(app.stores):
        for c in pipeline.list_all(workspace=ws):
            if any(j["status"] == "running" for j in jobs.list_jobs(c["id"], ws)):
                return True
    return False


def lifecycle(app: "R.App", life: Life, srv) -> None:
    """Self-refresh and the time limits, in one thread."""
    def files():
        return [Path(m.__file__) for m in HOT] + COLD + sorted(R.STATIC.iterdir())
    w = selfrefresh.Watcher(files)
    cold = {str(p) for p in COLD}
    next_jobs = 0.0
    life.version = int(time.time())
    while True:
        time.sleep(TICK)
        try:
            changed, err = w.changes()
            life.error = err or life.error if err else life.error
            if changed:
                reload_hot(changed, life)
                life.pending = life.pending or bool(cold & set(changed))
                life.version += 1
            busy = any(not t.done for t in list(app.turns.values()))
            if busy:
                life.touch()
            elif life.pending:
                reexec(app, life, srv)
            if time.time() >= next_jobs:
                next_jobs = time.time() + JOB_CHECK
                if jobs_running(app):
                    life.touch()
            s = life.stops_in()
            if s["seconds"] is not None and s["seconds"] <= 0:
                life.reason = s["why"]
                print(f"{time.strftime('%F %T')} stopping: {s['why']} limit reached", flush=True)
                srv.shutdown()
                return
        except Exception:
            print(traceback.format_exc(limit=4), flush=True)


class Handler(BaseHTTPRequestHandler):
    """Everything is in api.py, looked up on each request so a reload applies at once."""
    server_version = "moirai-agents-webui/0.1"
    protocol_version = "HTTP/1.0"
    app = None
    life = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        api.handle(self, self.app, self.life, "GET")

    def do_POST(self):
        api.handle(self, self.app, self.life, "POST")


def bind(host: str, port: int):
    fd = os.environ.pop("MOIRAI_AGENTS_WEBUI_FD", None)
    srv = ThreadingHTTPServer((host, port), Handler, bind_and_activate=False)
    srv.daemon_threads = True
    if fd:
        srv.socket.close()
        srv.socket = socket.socket(fileno=int(fd))
        srv.server_address = srv.socket.getsockname()
        srv.server_name, srv.server_port = host, srv.server_address[1]
        return srv
    for p in list(range(port, port + 50)) + [0]:
        try:
            srv.server_address = (host, p)
            srv.server_bind()
            srv.server_activate()
            return srv
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            srv.socket.close()
            srv.socket = socket.socket(srv.address_family, srv.socket_type)
            srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raise OSError("no free port")


def shutdown(app: "R.App", instance: str, grace: float = 10.0) -> None:
    """Stop every running turn, then kill anything else this server started (found by its instance tag)."""
    running = [t for t in list(app.turns.values()) if not t.done]
    print(f"{time.strftime('%F %T')} stopping {len(running)} running chat(s)", flush=True)
    for t in running:
        t.emit({"type": "error", "text": "the moirai-agents server is shutting down; this run was stopped"})
        t.stop()
    end = time.time() + grace
    for t in running:
        with t.cond:
            t.cond.wait_for(lambda: t.done, timeout=max(0.1, end - time.time()))
    left = R.procs.sweep(instance, grace=3.0)
    if left["processes"]:
        print(f"  cleaned {len(left['processes'])} leftover process(es)", flush=True)
    if R.REGISTRY is not None:
        R.REGISTRY.remove()
    print(f"{time.strftime('%F %T')} moirai-agents web UI stopped", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("MOIRAI_AGENTS_WEBUI_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MOIRAI_AGENTS_WEBUI_PORT", "8766")))
    a = ap.parse_args(argv)
    R.STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(R.STATE, 0o700)
    instance = os.environ.get(R.procs.TAG) or uuid.uuid4().hex[:16]
    os.environ[R.procs.TAG] = instance             # inherited by every process this server starts
    token = os.environ.get("MOIRAI_AGENTS_WEBUI_TOKEN") or R.stable_token(R.STATE)
    app = R.App(token, R.STATE)
    app.dash = dashboard.Dashboards()
    life = Life(app)
    Handler.app, Handler.life = app, life
    print(f"workspace: {R.WS} (mcp: {R.prepare_workspace(str(R.WS))})", flush=True)
    for who, ok, detail in R.start_bridges(R.STATE):
        print(f"  OpenAI bridge for {who}: {'running' if ok else 'NOT started: ' + detail}", flush=True)
    srv = bind(a.host, a.port)
    port = srv.server_address[1]
    host = os.uname().nodename if a.host in ("0.0.0.0", "") else a.host
    print(f"moirai-agents web UI: http://{host}:{port}/?token={token}", flush=True)
    print(f"  tunnel: ssh -L {port}:localhost:{port} {os.uname().nodename}", flush=True)
    print(f"  stops after {life.idle_s or 'no'} s idle, {life.max_s or 'no'} s at most", flush=True)
    R.REGISTRY = R.procs.Registry({"port": port, "url": f"http://{host}:{port}/", "instance": instance, "log": os.environ.get("MOIRAI_AGENTS_WEBUI_LOG"), "workspace": str(R.WS)})

    def on_signal(signum, _frame):
        print(f"{time.strftime('%F %T')} signal {signal.Signals(signum).name}: shutting down", flush=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    threading.Thread(target=lifecycle, args=(app, life, srv), daemon=True, name="lifecycle").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(app, instance)
        app.bridges.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
