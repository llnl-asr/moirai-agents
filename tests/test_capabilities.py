import json

import pytest

from moirai_agents.core import capstore as S
from moirai_agents.core import learning as L

TOOL = '''"""A demo."""
def register(mcp):
    @mcp.tool()
    def demo_ping(x: int = 1) -> dict:
        """Add one to x. Use to check things work. Returns {y}."""
        return {"y": x + 1}

    @mcp.tool()
    def demo_check(y: int = 2) -> dict:
        """Rule on demo_ping's result. Use after it. Returns {ok}."""
        return {"ok": y > 1}
'''
SKILL = """---
name: demo_cap
description: A demo capability.
---

## Rules
r
## Constraints
c
## Corner cases
c
## Corrector
c
## Self-heal
s
## Self-learn
l
"""
AGENT = {"description": "demo", "level": "level_1", "tools": ["Read"], "skills": ["demo_cap"]}
PVC = {"predictor": ["demo_ping"], "validator": ["demo_check"]}
CHECKS = [{"tool": "demo_ping", "args": {"x": 1}, "expect": {"json": {"y": 2}}}]


def new(**kw):
    a = dict(name="demo_cap", tool_code=TOOL, skill_md=SKILL, agent=AGENT, checks=CHECKS, description="d", why="w", pvc=PVC)
    a.update(kw)
    return S.propose(**a)


@pytest.mark.parametrize("name", sorted(p.name for p in S.BUNDLED.iterdir() if p.is_dir()))
def test_every_bundled_capability_passes_its_own_validators(name):
    r = S.verify(name)
    assert r["status"] == "ok", r


def test_everything_is_a_capability():
    from conftest import tools
    t = tools()
    assert len(t) == sum(len(m["tools"]) for _, _, m in S.all_caps().values())
    assert {"capability_new", "pipeline_new", "lesson_add", "query_dataframe", "moirai_baseline_run"} <= set(t)
    assert all(v["capability"] for v in t.values())


def test_propose_then_approve_goes_live_without_restart():
    tools = {}
    S.refresh(tools, force=True)
    r = new()
    assert r["status"] == "ok", r
    S.refresh(tools)
    assert "demo_ping" not in tools                  # nothing runs before a person approves
    assert S.apply(r["id"], False)["status"] == "error"
    assert S.apply(r["id"], True)["status"] == "ok"
    S.refresh(tools)
    assert tools["demo_ping"]["fn"](x=2) == {"y": 3}


@pytest.mark.parametrize("kw,needle", [
    ({"tool_code": "def register(mcp):\n  pass\n"}, "no tool"),
    ({"tool_code": TOOL + "\nimport os\nos.system('x')\n"}, "not allowed"),
    ({"tool_code": TOOL.replace("x + 1", "len('/usr/WS2/a') + x")}, "machine-specific"),
    ({"skill_md": SKILL.replace("## Self-heal\ns\n", "")}, "Self-heal"),
    ({"skill_md": SKILL.replace("## Corrector\nc\n", "")}, "Corrector"),
    ({"pvc": {"predictor": ["demo_ping"]}}, "validator"),
    ({"pvc": {"predictor": ["demo_ping"], "validator": ["nope"]}}, "does not define"),
    ({"pvc": {"predictor": ["demo_ping"], "validator": ["demo_ping"]}}, "judge its own output"),
    ({"agent": {**AGENT, "tools": ["Bash"]}}, "too wide"),
    ({"agent": {**AGENT, "level": "gpt-5.5"}}, "level"),
    ({"checks": [{"tool": "demo_ping", "args": {}, "expect": {"json": {"y": 99}}}]}, "verification failed"),
])
def test_bad_bundles_are_refused(kw, needle):
    r = new(**kw)
    assert r["status"] == "error" and needle in json.dumps(r), r


def test_a_twin_under_another_name_is_refused():
    S.apply(new()["id"], True)
    twin = TOOL.replace('"""A demo."""', '"""Other."""').replace("demo_ping", "demo_pong").replace("demo_check", "demo_chk").replace("Add one", "Increment")
    r = new(name="other_cap", tool_code=twin, skill_md=SKILL.replace("demo_cap", "other_cap"), agent={**AGENT, "skills": ["other_cap"]},
            checks=[{"tool": "demo_pong", "args": {}, "expect": {"contains": "y"}}], pvc={"predictor": ["demo_pong"], "validator": ["demo_chk"]})
    assert "fingerprint twin" in json.dumps(r)


def test_update_and_rollback_keep_history():
    S.apply(new()["id"], True)
    up = TOOL.replace("x + 1", "x + 2")
    r = S.propose("demo_cap", up, SKILL, AGENT, [{"tool": "demo_ping", "args": {"x": 1}, "expect": {"json": {"y": 3}}}], "d", "fix", kind="update")
    assert r["status"] == "ok" and "x + 2" in r["diff"]
    assert S.apply(r["id"], True)["version"] == 2
    assert S.rollback("demo_cap", 1, True)["version"] == 3
    assert "x + 1" in S.get("demo_cap")["code"]


def test_broken_tool_keeps_last_working_version():
    S.apply(new()["id"], True)
    tools = {}
    S.refresh(tools, force=True)
    d = S.all_caps()["demo_cap"][1]
    (d / "tool.py").write_text("def register(mcp):\n    raise RuntimeError('boom')\n")
    r = S.refresh(tools)
    assert "boom" in r["errors"]["demo_cap"]
    assert tools["demo_ping"]["fn"]() == {"y": 2}
    assert any(c["name"] == "demo_cap" and "boom" in (c["error"] or "") for c in S.list_capabilities()["active"])


def test_user_lesson_applies_now_agent_lesson_waits():
    r = L.add("prov_query", "Always cite the trace directory", "user")
    assert r["status"] == "applied"
    assert "cite the trace" in S.get("prov_query")["lessons"]
    assert S.all_caps()["prov_query"][0] == "user"   # the bundled copy is shadowed, not written
    assert L.add("prov_query", "No evidence", "agent")["status"] == "error"
    a = L.add("prov_query", "Index goes on local disk for speed", "agent", "run 12 log line 40")
    assert a["status"] == "pending" and "local disk" not in S.get("prov_query")["lessons"]
    L.decide(a["id"], True)
    assert "local disk" in S.get("prov_query")["lessons"]
    assert L.add("prov_query", "Always cite the trace directory!", "user")["status"] == "duplicate"
    assert "cite the trace" in L.context(["prov_query"])


def test_the_bundled_store_is_never_written():
    bundled = S.BUNDLED / "app_smoke" / "capability.json"
    before = bundled.read_text()
    S.disable("app_smoke")
    assert bundled.read_text() == before
    assert S.all_caps()["app_smoke"][0] == "user" and S.all_caps()["app_smoke"][2]["enabled"] is False


def test_workspace_renders_scoped_agents(tmp_path):
    from moirai_agents import workspace
    ws = tmp_path / "ws"
    workspace.bootstrap(ws)
    text = (ws / ".claude/agents/moirai_run.md").read_text()
    assert "mcp__moirai__moirai_baseline_run" in text and "mcp__moirai__capability_new" not in text
    assert "model: inherit" in text
    assert (ws / ".claude/skills/moirai_run/SKILL.md").exists()
    S.disable("app_smoke")
    workspace.sync(ws)
    assert not (ws / ".claude/agents/app_smoke.md").exists()
