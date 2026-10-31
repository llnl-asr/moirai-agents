"""Every HTTP route of the web UI. This module is reloaded when it changes, so it keeps no state: chats live in runtime.App,
the Dashboard's feeds in dashboard.Dashboards, the server's lifecycle in server.Life."""
from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import uuid
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import prompts
from . import runtime as R
from ..core import capstore, diagrams, jobs, learning, pipeline, provtrace
from .accounts import LABEL, PROVIDERS, ModelConfig
from .bridge import OPENAI_URL

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
COOKIE = "moirai_token"


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
    if u.path in ("/moirai.js", "/style.css", "/diagrams.js"):
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
    if u.path == "/api/consolidation":
        return send_json(h, getattr(app, "consolidation", None) or {"lessons": [], "capabilities": []})
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
