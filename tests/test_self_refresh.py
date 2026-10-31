"""Self-refresh and time limits, on real server processes."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from conftest import REPO, Mcp, Web


def mcp_env(tmp_path, root):
    return {**os.environ, "PYTHONPATH": str(root), "MOIRAI_AGENTS_MCP_POLL": "0.3", "MOIRAI_AGENTS_BUNDLED_CAPABILITIES": str(root / "moirai_agents/.agents/capabilities")}


def copy(tmp_path):
    root = tmp_path / "src"
    shutil.copytree(REPO / "src", root, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    return root


def names(m):
    return {t["name"] for t in m.rpc("tools/list")["tools"]}


def test_mcp_capability_edit_is_live_and_notified(tmp_path):
    root = copy(tmp_path)
    m = Mcp(mcp_env(tmp_path, root))
    m.rpc("initialize")
    assert "lesson_list" in names(m)
    f = root / "moirai_agents/.agents/capabilities/lessons/tool.py"
    f.write_text(f.read_text().replace("    @mcp.tool()\n    def lesson_list", '    @mcp.tool()\n    def lesson_ping() -> str:\n        """A new tool added while running. Returns ok."""\n        return "ok"\n\n    @mcp.tool()\n    def lesson_list'))
    assert m.wait_note()
    assert m.rpc("tools/call", {"name": "lesson_ping", "arguments": {}})["content"][0]["text"] == "ok"
    m.p.kill()


def test_mcp_code_edit_reexecutes_in_place_and_broken_edit_is_refused(tmp_path):
    root = copy(tmp_path)
    m = Mcp(mcp_env(tmp_path, root))
    m.rpc("initialize")
    pid = m.p.pid
    f = root / "moirai_agents/mcp_server.py"
    good = f.read_text()
    f.write_text("def broken(:\n" + good)
    time.sleep(1.5)
    assert "lesson_list" in names(m)                 # still the running code
    f.write_text(good.replace('"serverInfo": {"name": "moirai-agents"', '"serverInfo": {"name": "moirai-agents-2"'))
    assert m.wait_note()                             # the connection survived a change to the server itself
    assert m.p.pid == pid and m.rpc("initialize")["serverInfo"]["name"] == "moirai-agents-2"
    m.p.kill()


def test_web_static_and_api_refresh_while_running(tmp_path, web):
    w = web()
    v0 = w.get("/api/version")["version"]
    idx = w.pkg / "webui/static/style.css"
    idx.write_text(idx.read_text() + "\n/* changed */\n")
    w.wait(lambda: w.get("/api/version")["version"] > v0)
    assert b"changed" in urllib.request.urlopen(w.url("/style.css")).read()
    api = w.pkg / "webui/api.py"
    api.write_text(api.read_text().replace('"modes": list(prompts.MODES),', '"modes": list(prompts.MODES), "hot": 1,'))
    w.wait(lambda: w.get("/api/config").get("hot") == 1)
    good = api.read_text()
    api.write_text("def broken(:\n" + good)
    w.wait(lambda: w.get("/api/version")["error"])   # refused and shown on the page
    assert w.get("/api/config")["hot"] == 1
    api.write_text(good)
    w.wait(lambda: not w.get("/api/version")["error"])


def test_web_reexecutes_on_the_same_port_and_the_link_keeps_working(tmp_path, web):
    w = web()
    tok = "test-token"
    assert w.get("/api/me", tok)["owner"] is True
    pid = w.p.pid
    s = w.pkg / "webui/runtime.py"
    s.write_text(s.read_text() + "\nREFRESHED = 1\n")
    w.wait(lambda: "re-executing" in w.logp.read_text())
    w.wait(lambda: w.ok())
    assert w.p.pid == pid and w.p.poll() is None
    assert w.get("/api/me", tok)["owner"] is True


def test_web_stops_when_idle_and_leaves_nothing_behind(tmp_path, web):
    w = web(MOIRAI_AGENTS_WEBUI_IDLE_S="6", MOIRAI_AGENTS_WEBUI_MAX_S="0")
    tok = "test-token"
    s = w.get("/api/version")["stops"]
    assert s["why"] == "idle" and s["seconds"] <= 6 and s["warn"]
    w.get("/api/pipelines", tok)                     # page polls do not count as activity
    assert w.p.wait(20) == 0
    assert "idle limit reached" in w.logp.read_text()
    assert not list((tmp_path / "run").glob("*.json"))


def test_web_hard_limit_ignores_activity(tmp_path, web):
    w = web(MOIRAI_AGENTS_WEBUI_IDLE_S="0", MOIRAI_AGENTS_WEBUI_MAX_S="6")
    end = time.time() + 20
    while w.p.poll() is None and time.time() < end:
        try:
            w.post("/api/keepalive", {})
        except OSError:
            pass
        time.sleep(0.5)
    assert w.p.poll() == 0 and "maximum lifetime limit reached" in w.logp.read_text()


def test_dfdashboard_runs_behind_this_port_and_streams(tmp_path, web):
    fake = tmp_path / "fake-dfdashboard"
    fake.write_text(f"""#!{sys.executable}
import http.server, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/api/prov/stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for i in range(3):
                self.wfile.write(b"data: %d\\n\\n" % i); self.wfile.flush(); time.sleep(0.2)
            return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><head></head>" + self.path.encode())
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")
    fake.chmod(0o755)
    w = web(MOIRAI_AGENTS_DFDASHBOARD=str(fake))
    r = urllib.request.Request(w.url("/dash/?view=provenance&run=a%2Fb"), headers={"X-Moirai-Token": "test-token"})
    page = urllib.request.urlopen(r, timeout=30).read().decode()
    assert 'window.__DFTRACER__={apiBase:"/dash",base:"/dash"}' in page and "/?view=provenance&run=a%2Fb" in page
    r = urllib.request.Request(w.url("/dash/api/prov/stream?run=x"), headers={"X-Moirai-Token": "test-token"})
    s = urllib.request.urlopen(r, timeout=10)
    t0 = time.time()
    assert s.readline() == b"data: 0\n"             # delivered before the stream ends: not buffered
    assert time.time() - t0 < 0.3 + 2
    assert b"data: 2" in s.read()
    try:
        urllib.request.urlopen(w.url("/dash/"), timeout=5)
        raise AssertionError("no token was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    child = int(subprocess.check_output(["pgrep", "-f", str(fake)]).split()[0])
    w.stop()
    Web.wait(lambda: not os.path.exists(f"/proc/{child}"), 10)    # it goes down with the server


def test_analysis_traces_are_linked_where_dfdashboard_watches(tmp_path, monkeypatch):
    from conftest import tools
    from moirai_agents.core import pipeline as P
    tr = tmp_path / "elsewhere"
    tr.mkdir()
    (tr / "a_host.pfw.gz").write_bytes(gzip.compress(b'{"name":"step","cat":"moirai","ph":1,"pid":1,"ts":1,"dur":1,"args":{"relations":{}}}\n'))
    t = tools()
    pid = t["pipeline_new"]["fn"](kind="analysis", name="t", traces=str(tr))["id"]
    r = t["dashboard_open"]["fn"](pipeline=pid)
    assert r["status"] == "ok"
    c = P.load(pid)
    assert P.dash_run(c) == f"pipelines/{pid}/traces/analysis"
    assert (P.root() / pid / "traces/analysis/raw" / "elsewhere_a_host.pfw.gz").resolve() == (tr / "a_host.pfw.gz").resolve() or list((P.root() / pid / "traces/analysis/raw").iterdir())


def test_a_step_links_to_its_state_and_its_run(tmp_path, monkeypatch, web):
    from conftest import tools
    from moirai_agents.core import pipeline as P
    w = web()
    monkeypatch.setenv("MOIRAI_AGENTS_PIPELINES_DIR", str(tmp_path / "ws/workspaces/pipelines"))
    pid = tools()["pipeline_new"]["fn"](kind="analysis", name="t", traces="/t")["id"]
    raw = P.root() / pid / "traces/ingest/raw"
    P.record(pid, "ingest", status="running", outputs={"traces": str(raw)}, note="reading traces")
    row = next(s for s in w.get("/api/pipelines")["pipelines"][0]["stages"] if s["name"] == "ingest")
    assert row["run"] == f"pipelines/{pid}/traces/ingest"
    assert w.get(f"/api/pipeline/{pid}/ingest")["history"][0]["note"] == "reading traces"
    try:
        w.get(f"/api/pipeline/{pid}/nope")
        raise AssertionError("a missing step opened")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_a_server_started_inside_a_chat_leaves_that_chat_alive(tmp_path):
    env = {**os.environ, "MOIRAI_AGENTS_WEBUI_INSTANCE": "the-chat"}
    chat = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env)
    try:
        w = Web(tmp_path, MOIRAI_AGENTS_WEBUI_INSTANCE="the-chat")
        w.stop()
        time.sleep(0.5)
        assert chat.poll() is None
    finally:
        chat.kill()
        chat.wait()


def test_a_path_opens_directly_without_a_pipeline(tmp_path, web):
    w = web()
    tr = tmp_path / "my traces"
    tr.mkdir()
    (tr / "a.pfw.gz").write_bytes(gzip.compress(b"{}\\n"))
    run = w.post("/api/dashboard/open", {"path": str(tr)})["run"]
    raw = tmp_path / "ws/workspaces" / run / "raw"
    assert run.startswith("opened/") and raw.resolve() == tr.resolve()
    assert w.post("/api/dashboard/open", {"path": str(tr)})["run"] == run     # same path, same run
    (tr / "later.pfw.gz").write_bytes(gzip.compress(b"{}\\n"))
    assert (raw / "later.pfw.gz").exists()                                     # a file written later is seen
    nested = tmp_path / "tree/x"
    nested.mkdir(parents=True)
    (nested / "b.pfw.gz").write_bytes(gzip.compress(b"{}\\n"))
    for path in (str(tmp_path / "tree"), str(nested / "b.pfw.gz"), str(tmp_path / "tree/*/*.pfw.gz")):
        run = w.post("/api/dashboard/open", {"path": path})["run"]
        assert len(list((tmp_path / "ws/workspaces" / run / "raw").iterdir())) == 1
    try:
        w.post("/api/dashboard/open", {"path": str(tmp_path / "nothing")})
        raise AssertionError("an empty path opened")
    except urllib.error.HTTPError as e:
        assert e.code == 400 and b"no .pfw.gz" in e.read()


def test_a_placeholder_key_is_not_a_key(tmp_path, web):
    w = web(LIVAI_API_KEY="sk-...", MOIRAI_AGENTS_ENV_FILE=str(tmp_path / "no.env"))
    assert not w.get("/api/me")["model_access"]["providers"]


def test_first_requests_can_arrive_together(tmp_path, web):
    w = web(MOIRAI_AGENTS_OWNER_KEY="sk-ant-api-fake")
    with ThreadPoolExecutor(8) as pool:
        assert all(r["owner"] is True for r in pool.map(lambda _: w.get("/api/me"), range(8)))


def test_no_sign_in_but_the_url_token_is_required(tmp_path, web):
    w = web()
    assert w.get("/api/version")                      # the page can always ask this
    r = urllib.request.Request(w.url("/api/me"), headers={"X-Moirai-Token": "wrong"})
    try:
        urllib.request.urlopen(r, timeout=5)
        raise AssertionError("a wrong token was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    assert w.get("/api/me")["owner"] is True
    for path in ("/api/login", "/api/logout", "/api/password"):
        try:
            w.post(path, {})
            raise AssertionError(path)
        except urllib.error.HTTPError as e:
            assert e.code == 404


def test_static_files_are_well_formed():
    css = (REPO / "src/moirai_agents/webui/static/style.css").read_text()
    assert css.count("{") == css.count("}")          # one unclosed block silently swallows every rule after it
