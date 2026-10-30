"""Self-refresh and time limits, on real server processes."""
import gzip
import json
import os
import shutil
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


def test_dashboard_pushes_events_as_the_trace_grows(tmp_path, web):
    w = web()
    tok = "test-token"
    tr = tmp_path / "traces"
    tr.mkdir()
    pdir = tmp_path / "ws/workspaces/pipelines/demo-1"
    pdir.mkdir(parents=True)
    (pdir / "pipeline.json").write_text(json.dumps({"id": "demo-1", "kind": "analysis", "name": "demo", "created": 1, "stages": {}, "dashboard": {"traces": str(tr), "live": True}}))

    def member(i):
        return gzip.compress((json.dumps({"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": f"e{i}", "type": "t", "store": 2, "uri": f"u{i}"}}) + "\n" +
                              json.dumps({"name": "step", "cat": "moirai", "ph": 1, "pid": 1, "ts": i, "dur": 3, "args": {"relations": {"generated": [f"e{i}"]}}}) + "\n").encode())
    f = tr / "run_node1.pfw.gz"
    f.write_bytes(member(1))
    r = urllib.request.urlopen(urllib.request.Request(w.url("/api/dash/demo-1/stream"), headers={"X-Moirai-Token": tok}), timeout=10)

    def next_data():
        ev = None
        while True:
            line = r.readline().decode()
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
            if line.startswith("data:"):
                return ev, json.loads(line[5:])
    ev, snap = next_data()
    assert ev == "snapshot" and snap["counts"]["activities"] == 1 and snap["live"] is True
    t0 = time.time()
    with open(f, "ab") as fh:
        fh.write(member(2))
    while True:
        _, d = next_data()
        if d["items"]:
            break
    assert time.time() - t0 < 3
    assert {i["kind"] for i in d["items"]} == {"entity", "activity"} and d["counts"]["activities"] == 2
    assert w.get("/api/dash/demo-1/lineage?entity=e2", tok)["status"] == "ok"


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
