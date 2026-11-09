"""Every HTTP route of the web UI. This module is reloaded when it changes, so it keeps no state: chats live in runtime.App,
the Dashboard's feeds in dashboard.Dashboards, the server's lifecycle in server.Life."""
from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import tempfile
import time
import uuid
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import prompts
from . import runtime as R
from ..core import capstore, consolidate, diagrams, jobs, learning, pipeline, provtrace
from .accounts import LABEL, PROVIDERS, ModelConfig
from .bridge import OPENAI_URL

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
COOKIE = "moirai_token"



# Modules the routes import lazily are not in the server's hot list: reload them here when their file is newer than what is
# loaded (the process start, or the last reload), dependencies first, so an edit never leaves a new api.py calling an old one.
_PROC_START = os.stat("/proc/self").st_ctime if os.path.exists("/proc/self") else time.time()
_LOADED: dict = {}


def _fresh_wb():
    import importlib
    from ..core import aggregate, answers, evidence, gaps, liveinputs
    from . import workbench
    for m in (aggregate, answers, evidence, gaps, liveinputs, workbench):
        f = getattr(m, "__file__", "") or ""
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if mt > _LOADED.get(f, _PROC_START):
            if f in _LOADED or mt > _PROC_START:
                importlib.reload(m)
            _LOADED[f] = mt
    return workbench

def send_json(h, obj, code=200, cookie=None):
    b = json.dumps(obj, default=str).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    if cookie is not None:
        h.send_header("Set-Cookie", cookie)
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


VENDOR = {"cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.js", "jspdf.umd.min.js", "svg2pdf.umd.min.js"}      # static/vendor, see THIRD_PARTY.md
BOKEH_JS = {"bokeh.min.js", "bokeh-widgets.min.js", "bokeh-tables.min.js", "bokeh-api.min.js"}


def send_bokeh(h, name):
    """BokehJS from the installed bokeh package: the browser always gets the version that built the plot."""
    try:
        import bokeh
        p = Path(bokeh.__file__).resolve().parent / "server" / "static" / "js" / name
    except ImportError:
        return send_json(h, {"error": "bokeh is not installed in the server's Python"}, 404)
    if name not in BOKEH_JS or not p.is_file():
        return send_json(h, {"error": "not found"}, 404)
    b = p.read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", STATIC_TYPES[".js"])
    h.send_header("Cache-Control", "public, max-age=86400")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


def send_static(h, name, cookie=None):
    p = R.STATIC / name
    vendored = p.parent == R.STATIC / "vendor"       # third-party bundles (see static/vendor/THIRD_PARTY.md): pinned, so the browser may keep them
    if not p.is_file() or p.parent != R.STATIC and not vendored:
        return send_json(h, {"error": "not found"}, 404)
    b = p.read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", STATIC_TYPES.get(p.suffix, "application/octet-stream"))
    h.send_header("Cache-Control", "public, max-age=86400" if vendored else "no-cache")
    if cookie:
        h.send_header("Set-Cookie", cookie)
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


def set_cookie(tok):
    return f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/"


def body_of(h):
    n = int(h.headers.get("Content-Length") or 0)
    if n > R.UPLOAD_MAX_TOTAL * 4 // 3 + 1_000_000:
        h.rfile.read(n)
        return {"_too_big": True}
    return json.loads(h.rfile.read(n) or b"{}") if n else {}


def authorized(h, app, q=None):
    """There is no sign-in, but a login node is shared and anyone on it can reach 127.0.0.1: the printed URL carries a private token."""
    tok = h.headers.get("X-Moirai-Token") or ((q or {}).get("token") or [None])[0]
    if not tok:
        c = cookies.SimpleCookie(h.headers.get("Cookie", ""))
        tok = c[COOKIE].value if COOKIE in c else None
    return bool(tok) and secrets.compare_digest(tok, app.token)


def config():
    return {"models": R.MODELS, "default_model": os.environ.get("MOIRAI_AGENTS_MODEL", R.MODELS[0]), "modes": list(prompts.MODES),
            "providers": {p: LABEL[p] for p in PROVIDERS}, "openai_url": OPENAI_URL}


def status():
    c = R.find_claude()
    return {"claude": c["path"] if c["ok"] else None, "claude_version": c["version"], "claude_check": c, "providers": {p: LABEL[p] for p in PROVIDERS},
            "capabilities": len(capstore.all_caps()), "workspace": str(R.WS)}


def pipelines_view(user, session):
    out = []
    for c in pipeline.list_all(session or "", user["workspace"]):
        v = pipeline.summary(c)
        v["dash_run"] = pipeline.dash_run(c, user["workspace"])
        v["jobs"] = jobs.list_jobs(c["id"], user["workspace"])
        for r in v["stages"]:
            r["run"] = pipeline.traces_run(c["stages"][r["name"]]["outputs"].get("traces"), user["workspace"])
        out.append(v)
    return {"pipelines": out}


def stream_chat(h, app, user, sid, start):
    t = app.turns.get(f"{user['uid']}/{sid}")
    try:
        start = max(start, int(h.headers.get("Last-Event-ID") or 0))
    except ValueError:
        pass
    sse_open(h)
    if t is None:
        h.wfile.write(b"event: done\ndata: {}\n\n")
        return
    i = start
    try:
        while True:
            with t.cond:
                while i >= len(t.events) and not t.done:
                    t.cond.wait(timeout=15)
                    if i >= len(t.events) and not t.done:
                        h.wfile.write(b": keepalive\n\n")
                        h.wfile.flush()
                evs, done = t.events[i:], t.done
            for ev in evs:
                i += 1
                h.wfile.write(f"id: {i}\ndata: {json.dumps(ev)}\n\n".encode())
            h.wfile.flush()
            if done and i >= len(t.events):
                h.wfile.write(b"event: done\ndata: {}\n\n")
                h.wfile.flush()
                return
    except (BrokenPipeError, ConnectionResetError):
        return


def sse_open(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    h.wfile.write(b": stream open\n\n")
    h.wfile.flush()


def ws_tunnel(h, app, user, path):
    """A websocket to the dfdashboard child (its Panel plots keep a live session): forward the upgrade request, then copy
    bytes both ways until either side closes."""
    import socket
    import threading
    port = app.dash.ensure(str(pipeline.dash_root(user["workspace"])))
    if port is None:
        return send_json(h, {"error": app.dash.error}, 503)
    try:
        up = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError as e:
        return send_json(h, {"error": f"dashboard not reachable: {e}"}, 502)
    up.settimeout(None)
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    lines += [f"{k}: {v}" for k, v in h.headers.items() if k.lower() not in ("host", "cookie", "x-moirai-token")]
    up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    down = h.connection
    h.close_connection = True

    def pump(src, dst):
        try:
            while True:
                b = src.recv(65536)
                if not b:
                    break
                dst.sendall(b)
        except OSError:
            pass
        finally:
            for x in (src, dst):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    t = threading.Thread(target=pump, args=(up, down), daemon=True)
    t.start()
    pump(down, up)
    t.join(timeout=5)
    up.close()


def proxy(h, app, user, path, method="GET", body=None):
    """Forward to the dfdashboard child. It streams, so bytes go through as they come; a page learns its API base from the injected config."""
    port = app.dash.ensure(str(pipeline.dash_root(user["workspace"])))
    if port is None:
        return send_json(h, {"error": app.dash.error}, 503)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=None)
    headers = {k: v for k, v in h.headers.items() if k.lower() not in ("host", "cookie", "x-moirai-token", "accept-encoding", "connection")}
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    html = (r.getheader("Content-Type") or "").startswith("text/html")
    h.send_response(r.status)
    for k, v in r.getheaders():
        if k.lower() not in ("transfer-encoding", "connection", "content-length", "server", "date"):
            h.send_header(k, v)
    h.send_header("Connection", "close")
    if html:
        data = r.read().replace(b"<head>", b'<head><script>window.__DFTRACER__={apiBase:"/dash",base:"/dash"}</script>', 1)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    else:
        h.end_headers()
        try:
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                h.wfile.write(chunk)
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
    c.close()
    h.close_connection = True


def get_route(h, app, life, u, q):
    if u.path in ("/", "/index.html"):
        return send_static(h, "index.html", set_cookie(app.token) if authorized(h, app, q) else None)
    if u.path in ("/moirai.js", "/style.css", "/diagrams.js", "/views.js", "/wb.js"):
        return send_static(h, u.path[1:])
    if u.path.startswith("/vendor/"):
        name = u.path[len("/vendor/"):]
        if name.startswith("bokeh/"):
            return send_bokeh(h, name[len("bokeh/"):])
        return send_static(h, "vendor/" + name) if name in VENDOR else send_json(h, {"error": "not found"}, 404)
    if u.path == "/api/version":
        return send_json(h, life.snapshot())
    if not authorized(h, app, q):
        return send_json(h, {"error": "open the link the server printed when it started"}, 401)
    if u.path == "/api/config":
        return send_json(h, config())
    user = app.local_user()
    st = app.store(user)
    if u.path == "/api/me":
        cfgd = ModelConfig.public(ModelConfig(Path(user["model_path"])).load())
        return send_json(h, {"name": user["name"], "owner": True, "models": user.get("models") or [], "provider": user.get("provider"), "cred": user.get("cred"),
                             "model": user.get("model"), "model_access": cfgd, "needs_model": not user.get("provider"), "workspace": user["workspace"]})
    if u.path == "/api/model":
        return send_json(h, ModelConfig.public(ModelConfig(Path(user["model_path"])).load()))
    if u.path == "/api/status":
        if q.get("recheck"):
            R.find_claude(refresh=True)
        return send_json(h, status())
    if u.path == "/api/sessions":
        out = st.list()
        for x in out:
            t = app.turns.get(f"{user['uid']}/{x['id']}")
            x["running"] = bool(t and not t.done)
        return send_json(h, out)
    if u.path.startswith("/api/session/"):
        sid = u.path.rsplit("/", 1)[1]
        t = app.turns.get(f"{user['uid']}/{sid}")
        s = t.snapshot() if t is not None and not t.done else st.get(sid)
        s and s.pop("claude_session", None)
        return send_json(h, s or {"error": "not found"}, 200 if s else 404)
    if u.path == "/api/pipelines":
        return send_json(h, pipelines_view(user, q.get("session", [""])[0]))
    if u.path.startswith("/api/pipeline/"):
        pid, name = u.path.split("/")[3:5]
        return send_json(h, pipeline.stage_state(pipeline.load(pid, user["workspace"]), name))
    if u.path.startswith("/api/diagram/"):
        pid, name = u.path.split("/")[3:5]
        try:
            return send_json(h, diagrams.read(pipeline.load(pid, user["workspace"]), name))
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 404)
    if u.path == "/api/job/detail":
        flux = q.get("flux", [""])[0]
        if flux:
            return send_json(h, jobs.flux_detail(flux))
        try:
            return send_json(h, jobs.detail(q.get("pipeline", [""])[0], q.get("job", [""])[0], user["workspace"]))
        except (OSError, ValueError, pipeline.PipelineError) as e:
            return send_json(h, {"error": str(e)}, 404)
    if u.path == "/api/auto":
        return send_json(h, {"on": pipeline.auto_mode(q.get("session", [""])[0], user["workspace"])})
    if u.path == "/api/background":
        return send_json(h, {"agents": app.background.agents() if getattr(app, "background", None) else []})
    if u.path.startswith("/api/background/run/"):
        return send_json(h, app.background.run(u.path.rsplit("/", 1)[1]))
    if u.path == "/api/history":
        sid = q.get("session", [""])[0]
        t = app.turns.get(f"{user['uid']}/{sid}")
        s = t.snapshot() if t is not None and not t.done else st.get(sid)
        if not s:
            return send_json(h, {"error": "no such chat"}, 404)
        from . import history
        return send_json(h, history.read(user, s, q.get("call", [None])[0]))
    if u.path == "/api/mcp":
        from . import mcp
        return send_json(h, mcp.state(user["workspace"], sum(1 for t in list(app.turns.values()) if not t.done)))
    if u.path.startswith("/api/workbench"):
        W = _fresh_wb()
        pid = q.get("pipeline", [""])[0]
        try:
            if u.path == "/api/workbench":
                busy = {t.session.get("id") for t in list(app.turns.values()) if not t.done}     # chats whose agent is working now
                return send_json(h, W.items(user["workspace"], busy))
            if u.path == "/api/workbench/card":
                return send_json(h, W.card(user["workspace"], pid))
            if u.path == "/api/workbench/live":
                return send_json(h, W.live(user["workspace"], pid))
            if u.path == "/api/workbench/render":
                from ..core import liveinputs as LI
                spec = pipeline.load(pid, user["workspace"]).get("live_inputs")
                if not spec:
                    return send_json(h, {"status": "error", "problems": ["no live-run inputs"]})
                try:
                    vals = json.loads(q.get("values", ["{}"])[0] or "{}")
                except ValueError:
                    vals = {}
                return send_json(h, LI.render(spec, vals if isinstance(vals, dict) else {}))
            if u.path == "/api/workbench/aggregate":
                return send_json(h, W.aggregate(user["workspace"], pid))
            if u.path == "/api/workbench/graph":
                return send_json(h, W.graph(user["workspace"], pid, q.get("since", [""])[0]))
            if u.path == "/api/workbench/explain":        # an answer in four parts, by the query_explain capability (no model)
                from ..core import capstore as CS
                aid = q.get("id", [""])[0]
                try:
                    r = CS.call("query_explain", "query_explain", pipeline=pid, answer_id=aid)
                    r["checks"] = CS.call("query_explain", "query_explain_validate", pipeline=pid, answer_id=aid).get("parts")   # against the traces
                    r["confidence"] = CS.call("answer_confidence", "confidence_score", pipeline=pid, answer_id=aid)
                    return send_json(h, r)
                except Exception as e:
                    return send_json(h, {"error": f"{type(e).__name__}: {e}"})
            if u.path == "/api/workbench/confidence":     # any answer, also an agent-written one
                from ..core import capstore as CS
                try:
                    return send_json(h, CS.call("answer_confidence", "confidence_score", pipeline=pid, answer_id=q.get("id", [""])[0]))
                except Exception as e:
                    return send_json(h, {"error": f"{type(e).__name__}: {e}"})
            if u.path == "/api/workbench/answers":
                from ..core import answers
                return send_json(h, {"answers": answers.listing(pid, user["workspace"]), "pins": answers.pins(pid, user["workspace"])})
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path == "/api/consolidation":
        return send_json(h, consolidate.unkept(getattr(app, "consolidation", None) or {"lessons": [], "capabilities": []}))
    if u.path == "/api/submitted":
        return send_json(h, {"jobs": jobs.submitted(float(q.get("since", ["0"])[0] or 0))})
    if u.path == "/api/capabilities":
        return send_json(h, capstore.list_capabilities())
    if u.path.startswith("/api/capability/"):
        return send_json(h, capstore.get(u.path.rsplit("/", 1)[1]))
    if u.path == "/api/lessons":
        return send_json(h, {"lessons": learning.listing()})
    if u.path.startswith("/api/stream/"):
        return stream_chat(h, app, user, u.path.rsplit("/", 1)[1], int(q.get("from", ["0"])[0]))
    if u.path == "/dash" or u.path.startswith(("/dash/", "/assets/")):
        target = u.path[5:] if u.path.startswith("/dash") else u.path
        return proxy(h, app, user, (target or "/") + ("?" + u.query if u.query else ""))
    if u.path.startswith(("/panel/", "/panel-dist/")):          # dfdashboard's Panel plots: absolute paths and a live websocket
        target = u.path + ("?" + u.query if u.query else "")
        if "websocket" in (h.headers.get("Upgrade") or "").lower():
            return ws_tunnel(h, app, user, target)
        return proxy(h, app, user, target)
    return send_json(h, {"error": "not found"}, 404)


def model_post(h, app, user, path, body):
    mc = ModelConfig(Path(user["model_path"]))
    prov = body.get("provider") or ""
    if path == "/api/model/active":
        try:
            d = mc.activate(prov)
        except KeyError:
            return send_json(h, {"error": f"{prov} is not configured yet"}, 400)
    elif path == "/api/model/remove":
        d = mc.remove(prov)
    elif path == "/api/model":
        old = mc.load()["providers"].get(prov) or {}
        key = (body.get("key") or "").strip() or old.get("key") or ""
        if prov == "anthropic" and body.get("use_login"):
            key = R.OWNER_LOGIN
        v = R.verify_provider(prov, key, body.get("model") or old.get("model"), body.get("base_url") or old.get("base_url"), user["owner"])
        if not v["ok"]:
            return send_json(h, {"error": v["error"]}, 400)
        d = mc.put(prov, {"key": key, "cred": v["cred"], "base_url": v["base_url"], "stream": v["stream"], "model": v["model"], "models": v["models"], "verified": R._now()},
                   activate=body.get("activate", True))
    else:
        return send_json(h, {"error": "not found"}, 404)
    app.refresh_models()
    return send_json(h, dict(ModelConfig.public(d), ok=True))


def chat_pdf(h, body):
    """Export PDF: the snapshot the page sent, through whichever version of the chat_pdf capability is active."""
    if body.get("_too_big"):
        return send_json(h, {"error": "this chat is too large to export"}, 413)
    with tempfile.TemporaryDirectory(prefix="moirai-chat-pdf-") as d:
        snap, pdf = Path(d) / "chat.html", str(Path(d) / "chat.pdf")
        snap.write_text(body.get("html") or "")
        r = capstore.call("chat_pdf", "chat_pdf_render", snapshot=str(snap), out=pdf, title=body.get("title") or "moirai-agents chat", css=str(R.STATIC / "style.css"), when=body.get("when") or "")
        r = capstore.call("chat_pdf", "chat_pdf_check", pdf=pdf) if r["status"] == "ok" else r
        if r["status"] != "ok":
            return send_json(h, {"error": r["error"]}, 500)
        b = Path(pdf).read_bytes()
    h.send_response(200)
    h.send_header("Content-Type", "application/pdf")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


def chat(h, app, user, st, body):
    tkey = lambda sid: f"{user['uid']}/{sid}"   # noqa: E731
    if body.get("_too_big"):
        return send_json(h, {"error": f"upload too large (max {R.UPLOAD_MAX_TOTAL >> 20} MB per message)"}, 413)
    files = []
    try:
        for f in body.get("files") or []:
            data = base64.b64decode(f.get("data") or "", validate=True)
            if len(data) > R.UPLOAD_MAX_FILE:
                return send_json(h, {"error": f"{f.get('name')}: larger than {R.UPLOAD_MAX_FILE >> 20} MB"}, 413)
            files.append((str(f.get("name") or "file"), data))
    except (ValueError, TypeError):
        return send_json(h, {"error": "could not decode the uploaded file"}, 400)
    if not (body.get("message") or "").strip() and files:
        body["message"] = "Please look at the attached file(s)."
    if not user.get("provider"):
        return send_json(h, {"error": "choose and verify your model access first (Model settings)", "needs_model": True}, 400)
    sid = body.get("session") or uuid.uuid4().hex[:12]
    s = st.get(sid) or {"id": sid, "created": R._now(), "turns": [], "user": user["name"], "title": (body.get("message") or "")[:60], "mode": body.get("mode") or "ask",
                        "model": body.get("model") or os.environ.get("MOIRAI_AGENTS_MODEL", R.MODELS[0]), "app_dir": None}
    if body.get("model"):
        s["model"] = body["model"]
    allowed = user.get("models") or R.provider_models(user.get("provider", "livai"))
    if s.get("model") not in allowed:
        s["model"] = allowed[0]
    if body.get("mode"):
        s["mode"] = body["mode"]
    old = app.turns.get(tkey(sid))
    msg = body.get("message") or ""
    if old and not old.done:
        if old.steer(msg, files):
            return send_json(h, {"session": sid, "steered": True})
        with old.cond:
            old.cond.wait_for(lambda: old.done, timeout=60)
        s = st.get(sid) or s
    cc = R.find_claude()
    if not cc["ok"]:
        cc = R.find_claude(refresh=True)
    if not cc["ok"]:
        return send_json(h, {"error": "The agent cannot start: " + cc["error"], "claude": cc, "owner": user["owner"]}, 503)
    try:
        t = R.Turn(s, msg, st, app.state, user, files)
    except RuntimeError as e:
        return send_json(h, {"error": str(e)}, 503)
    app.turns[tkey(sid)] = t
    st.put(s)
    t.start()
    return send_json(h, {"session": sid})


def post_route(h, app, life, u, body):
    if not authorized(h, app):
        return send_json(h, {"error": "open the link the server printed when it started"}, 401)
    user = app.local_user()
    R.touch_activity(user["workspace"])
    st = app.store(user)
    tkey = lambda sid: f"{user['uid']}/{sid}"   # noqa: E731
    if u.path == "/api/keepalive":
        return send_json(h, {"ok": True})
    if u.path.startswith("/api/model"):
        return model_post(h, app, user, u.path, body)
    if u.path in ("/api/claude/path", "/api/claude/install"):
        r = R.use_claude_path(body.get("path") or "") if u.path.endswith("path") else R.install_claude()
        return send_json(h, r, 200 if r.get("ok") else 400)
    if u.path == "/api/chat":
        return chat(h, app, user, st, body)
    if u.path == "/api/auto":
        return send_json(h, {"on": pipeline.set_auto_mode(body.get("session", ""), bool(body.get("on")), user["workspace"])})
    if u.path == "/api/stop":
        t = app.turns.get(tkey(body.get("session") or ""))
        if t is None or t.done:
            return send_json(h, {"ok": True})
        t.stop()
        with t.cond:
            t.cond.wait_for(lambda: t.done, timeout=30)
        return send_json(h, {"ok": True, "queued": 0})
    if u.path in ("/api/capability/decide", "/api/lesson/decide"):
        if u.path.startswith("/api/lesson"):
            return send_json(h, learning.decide(body.get("id", ""), bool(body.get("approve")), user["name"]))
        if body.get("approve"):
            return send_json(h, capstore.apply(body.get("id", ""), True, user["name"]))
        return send_json(h, capstore.reject(body.get("id", ""), body.get("note", ""), user["name"]))
    if u.path == "/api/lesson/revise":
        return send_json(h, learning.revise(body.get("id", ""), body.get("text", ""), body.get("capability", ""), body.get("note", ""), bool(body.get("approve")), user["name"]))
    if u.path == "/api/capability/refine":
        edit = (body.get("skill", ""), body.get("code", ""), body.get("note", ""), user["name"])
        r = capstore.refine(body["id"], *edit) if body.get("id") else capstore.revise(body.get("name", ""), *edit)
        return send_json(h, capstore.apply(r["id"], True, user["name"]) if body.get("approve") and r["status"] == "ok" else r)
    if u.path == "/api/chat/pdf":
        return chat_pdf(h, body)
    if u.path == "/api/job/stop":
        return send_json(h, jobs.stop(body.get("pipeline", ""), body.get("job", "")))
    if u.path == "/api/delete":
        ids = body.get("sessions") or [body.get("session", "")]
        if body.get("archived_all"):
            ids = [x["id"] for x in st.list() if x.get("archived")]
        for sid in ids:
            t = app.turns.get(tkey(sid))
            if t is not None and not t.done:
                t.stop()
                with t.cond:
                    t.cond.wait_for(lambda: t.done, timeout=30)
            st.delete(sid)
        return send_json(h, {"ok": True, "deleted": len(ids)})
    if u.path == "/api/background/start":
        bg = getattr(app, "background", None)
        if bg is None:
            return send_json(h, {"error": "background agents are not running on this server"}, 503)
        a = body.get("agent", "")
        if a == "learner":
            r = bg.learn_once(force=True)
        elif a in ("lessons", "capabilities", "all"):
            r = bg.consolidate_once(consolidate.report(), force=a)
            r = (r.get(a) if a != "all" else r) or {"status": "nothing to consolidate"}
        else:
            return send_json(h, {"error": "agent is learner, lessons or capabilities"}, 400)
        if isinstance(r, str):
            r = {"status": r}
        return send_json(h, {**r, "agents": bg.agents()})
    if u.path in ("/api/mcp/check", "/api/mcp/restart"):
        from . import mcp
        name = body.get("name") or "all"
        if u.path.endswith("/check"):
            r = {"status": "ok", "checks": mcp.check(user["workspace"], list(mcp.specs(user["workspace"])) if name == "all" else [name])}
        else:
            r = mcp.restart(user["workspace"], name)
            R.wlog("mcp-restart", server=name, ok={n: c.get("ok") for n, c in (r.get("checks") or {}).items()})
        return send_json(h, {**r, **mcp.state(user["workspace"], sum(1 for t in list(app.turns.values()) if not t.done))},
                         400 if r.get("error") else 200)
    if u.path == "/api/workbench/gaps":           # gaps a computed answer showed: kept on the app, to be fixed in its annotation phase
        _fresh_wb()
        from ..core import gaps as G
        try:
            ids = body.get("gaps") if isinstance(body.get("gaps"), list) else [body.get("gap", "")]
            opened = [G.open_gap(body.get("pipeline", ""), str(body.get("answer", "")), str(g)) for g in ids if g]
            if not opened:
                return send_json(h, {"error": "no gap given"}, 400)
            return send_json(h, {"app": opened[0]["app"], "chat": opened[0]["chat"], "gaps": [{k: g.get(k) for k in ("id", "kind", "subject", "detail")} for g in opened]})
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path in ("/api/workbench/ask", "/api/workbench/rerun"):      # computed answers: TraceViewer queries, no model
        _fresh_wb()
        from ..core import evidence as E
        try:
            if u.path == "/api/workbench/ask":
                steps = body.get("steps")
                rec = E.ask(body.get("pipeline", ""), str(body.get("question", ""))[:400], steps if isinstance(steps, list) else [], chose="you", chat=body.get("session", ""))
                return send_json(h, {"id": rec["id"], "headline": rec["result"]["headline"]})
            return send_json(h, E.rerun(body.get("pipeline", ""), str(body.get("id", ""))))
        except (E.PlanError, pipeline.PipelineError) as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path == "/api/workbench/pin":
        from ..core import answers
        try:
            return send_json(h, {"on": answers.pin(body.get("pipeline", ""), str(body.get("id", "")), bool(body.get("on", True)), user["workspace"]),
                                 "pins": answers.pins(body.get("pipeline", ""), user["workspace"])})
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path in ("/api/workbench/live", "/api/workbench/querychat"):
        W = _fresh_wb()
        allowed = user.get("models") or R.provider_models(user.get("provider", "livai"))
        model = os.environ.get("MOIRAI_AGENTS_MODEL", allowed[0]) if os.environ.get("MOIRAI_AGENTS_MODEL") in allowed else allowed[0]
        sid = W.new_chat_id()
        try:
            if u.path == "/api/workbench/live":          # a live run of an annotated app, with its own run chat
                c = W.new_live(user["workspace"], body.get("parent", ""), body.get("name", ""), body.get("command", ""), sid,
                               body.get("values") if isinstance(body.get("values"), dict) else None)
                st.put({"id": sid, "created": R._now(), "updated": R._now(), "turns": [], "user": user["name"], "title": c["name"],
                        "mode": "run", "app_dir": None, "folder": "live runs", "model": model})
                return send_json(h, {"session": sid, "pipeline": c["id"], "command": c["command"], "inputs": c.get("inputs") or {}})
            c = pipeline.load(body.get("pipeline", ""), user["workspace"])     # the Query view's chat of a pipeline: made once, kept
            if c["kind"] == "analysis":
                return send_json(h, {"session": c.get("chat_session") or "", "pipeline": c["id"]})
            if c.get("query_chat") and st.get(c["query_chat"]):
                return send_json(h, {"session": c["query_chat"], "pipeline": c["id"]})
            st.put({"id": sid, "created": R._now(), "updated": R._now(), "turns": [], "user": user["name"], "title": f"Query: {c['name']}",
                    "mode": "ask", "pipeline": c["id"], "query": True, "app_dir": None, "folder": "queries", "model": model})
            pipeline.update(c["id"], lambda x: x.update(query_chat=sid))
            return send_json(h, {"session": sid, "pipeline": c["id"]})
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path == "/api/workbench/dataset":
        W = _fresh_wb()
        sid = W.new_chat_id()
        try:
            c = W.open_dataset(user["workspace"], body.get("path", ""), body.get("name", ""), sid)
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
        allowed = user.get("models") or R.provider_models(user.get("provider", "livai"))
        st.put({"id": sid, "created": R._now(), "updated": R._now(), "turns": [], "user": user["name"], "title": c["name"],
                "mode": "analysis", "pipeline": c["id"], "app_dir": None, "folder": "datasets",
                "model": os.environ.get("MOIRAI_AGENTS_MODEL", allowed[0]) if os.environ.get("MOIRAI_AGENTS_MODEL") in allowed else allowed[0]})
        return send_json(h, {"session": sid, "pipeline": c["id"], "card": W.card(user["workspace"], c["id"])})
    if u.path == "/api/rename":
        sid = body.get("session", "")
        if not st.rename(sid, body.get("title", "")):
            return send_json(h, {"error": "could not rename: the chat is gone or the name is empty"}, 400)
        t = app.turns.get(tkey(sid))
        if t is not None and not t.done:
            t.session["title"] = " ".join(body["title"].split())[:120]    # the running turn saves its own copy when it ends
        return send_json(h, {"ok": True})
    if u.path in ("/api/pin", "/api/folder", "/api/folder/rename"):
        def live(sid, **kv):        # a running turn saves its own copy of the chat when it ends
            t = app.turns.get(tkey(sid))
            if t is not None and not t.done:
                for k, v in kv.items():
                    t.session.pop(k, None) if v in (None, "", False) else t.session.__setitem__(k, v)
        def fname(x):
            return " ".join(str(x or "").replace("/", " ").split())[:40]      # one level only: no path separators
        if u.path == "/api/pin":
            ids = body.get("sessions") or [body.get("session", "")]
            v = R._now() if body.get("on", True) is not False else None
            for sid in ids:
                live(sid, pinned=v)
            return send_json(h, {"ok": True, "changed": sum(st.set_fields(sid, pinned=v) for sid in ids)})
        if u.path == "/api/folder":
            ids = body.get("sessions") or [body.get("session", "")]
            f = fname(body.get("folder"))
            for sid in ids:
                live(sid, folder=f or None)
            return send_json(h, {"ok": True, "changed": sum(st.set_fields(sid, folder=f or None) for sid in ids)})
        a, b = fname(body.get("from")), fname(body.get("to"))
        if not a or not b:
            return send_json(h, {"error": "a folder needs a name"}, 400)
        ids = [x["id"] for x in st.list() if x.get("folder") == a]
        for sid in ids:
            live(sid, folder=b)
        return send_json(h, {"ok": True, "changed": sum(st.set_fields(sid, folder=b) for sid in ids)})
    if u.path == "/api/archive":
        on = body.get("archived", True) is not False
        ids = body.get("sessions") or [body.get("session", "")]
        if body.get("all_active"):
            ids = [x["id"] for x in st.list() if not x.get("archived") and not ((app.turns.get(tkey(x["id"])) or R._Done).done is False)]
        return send_json(h, {"ok": True, "changed": sum(st.archive(sid, on) for sid in ids)})
    if u.path == "/api/feedback":
        return send_json(h, {"ok": True})
    if u.path in ("/api/diagram/save", "/api/diagram/reset"):
        try:
            if u.path.endswith("save"):
                return send_json(h, diagrams.save_edit(body.get("pipeline", ""), body.get("name", ""), body.get("graph"), user["workspace"]))
            return send_json(h, diagrams.reset_edit(body.get("pipeline", ""), body.get("name", ""), user["workspace"]))
        except pipeline.PipelineError as e:
            return send_json(h, {"error": str(e)}, 400)
    if u.path == "/api/dashboard/open":
        run = pipeline.open_traces(body.get("path") or "", user["workspace"])
        app.dash.wait_run(str(pipeline.dash_root(user["workspace"])), run)
        return send_json(h, {"run": run})
    if u.path.startswith("/dash/"):
        return proxy(h, app, user, u.path[5:] + ("?" + u.query if u.query else ""), "POST", json.dumps(body).encode())
    return send_json(h, {"error": "not found"}, 404)


def handle(h, app, life, method):
    u = urlparse(h.path)
    try:
        if method == "GET":
            return get_route(h, app, life, u, parse_qs(u.query))
        life.touch()                               # a person did something: page polls do not count
        return post_route(h, app, life, u, body_of(h))
    except (BrokenPipeError, ConnectionResetError):
        return
    except pipeline.PipelineError as e:
        return send_json(h, {"error": str(e)}, 400)
