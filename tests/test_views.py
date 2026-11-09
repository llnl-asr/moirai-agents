"""The three views' backend: live pipelines (a long run under an annotated app), the aggregated entity-relationship graph
and its diff against the spec (the Annotate view's check), and the endpoints the views read."""
import gzip
import json
from pathlib import Path

import pytest

from moirai_agents.core import aggregate as G
from moirai_agents.core import answers as A
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

REPO = Path(__file__).resolve().parents[1]
SPEC = {"entity_types": [{"type": "raw", "role": "input"}, {"type": "clean", "role": "intermediate"}, {"type": "plot", "role": "output"}],
        "scientific_artifacts": ["plot"],
        "activities": [{"name": "prep", "used": [{"type": "raw"}], "generated": [{"type": "clean"}]},
                       {"name": "draw", "used": [{"type": "clean"}], "generated": [{"type": "plot"}, {"type": "thumb", "when": "thumbs on"}]}]}


def member(*recs):
    return gzip.compress("".join(json.dumps(r) + "\n" for r in recs).encode())


def et(t, role):
    return {"name": "ET", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"type": t, "role": role, "description": t}}


def eh(i, t):
    return {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": i, "type": t, "uri": f"/data/{i}"}}


def act(name, ts, **rel):
    return {"name": name, "cat": "moirai", "ph": 1, "pid": 1, "ts": ts, "dur": 5, "args": {"relations": rel}}


def traces(tmp_path, *extra):
    d = tmp_path / "traces" / "raw"
    d.mkdir(parents=True)
    (d / "a.pfw.gz").write_bytes(member(et("raw", 1), et("clean", 3), et("plot", 2), eh("r", "raw"), eh("c", "clean"), eh("p", "plot"),
                                        act("prep", 1, used=["r"], generated=["c"]), act("draw", 2, used=["c"], generated=["p"]), *extra))
    return d


def integrated_app(tmp_path, spec=SPEC, name="app"):
    run = P.new("run", name, source=str(tmp_path), command="python app.py")
    P.update(run["id"], lambda c: c["stages"]["integrate"].update(status="passed", outputs={"env": "/e/moirai_env.sh", "annotated": str(tmp_path)}))
    P.record(run["id"], "annotate", outputs={"spec": spec})
    return run


def test_the_diff_marks_matched_conditional_and_unexpected(tmp_path):
    g = T.read(str(traces(tmp_path, et("log", 2), eh("x", "log"), act("draw", 3, used=["c"], generated=["x"]))))   # draw also wrote a log: not promised
    d = G.diff(G.expected(SPEC), G.runtime(g))
    st = {e["id"]: e["status"] for e in d["edges"]}
    assert st["used|t:raw|a:prep"] == st["generated|a:prep|t:clean"] == st["generated|a:draw|t:plot"] == "matched"
    assert st["generated|a:draw|t:thumb"] == "conditional"
    assert st["generated|a:draw|t:log"] == "unexpected"
    nodes = {n["id"]: n for n in d["nodes"]}
    assert nodes["t:thumb"]["status"] == "conditional" and nodes["t:log"]["status"] == "unexpected"
    assert nodes["a:draw"]["count"] == 2 and nodes["t:raw"]["count"] == 1
    assert d["clean"] and d["unexpected_types"] == ["log"] and d["summary"] == {"matched": 4, "missing": 0, "conditional": 1, "unexpected": 1}
    assert d["conditional"] == ["draw generated thumb"] and d["unexpected"] == ["draw generated log"]


def test_the_diff_names_what_was_promised_and_never_recorded(tmp_path):
    spec = {**SPEC, "activities": SPEC["activities"] + [{"name": "save", "used": [{"type": "plot"}], "generated": [{"type": "archive"}]}]}
    d = G.diff(G.expected(spec), G.runtime(T.read(str(traces(tmp_path)))))
    assert not d["clean"] and d["missing_types"] == ["archive"] and "save generated archive" in d["missing"] and "save used plot" in d["missing"]


def test_a_live_run_belongs_to_an_annotated_app(tmp_path):
    with pytest.raises(P.PipelineError, match="parent"):
        P.new("live", "x")
    bare = P.new("run", "bare", source=str(tmp_path), command="python app.py")
    with pytest.raises(P.PipelineError, match="integrate"):
        P.new("live", "app-live", parent=bare["id"])
    run = integrated_app(tmp_path)
    L = P.new("live", "app-live", parent=run["id"])
    assert list(L["stages"]) == ["live_run", "publish_data"] and L["parent"] == run["id"] and L["command"] == "python app.py"
    assert P.origin(L)["id"] == run["id"] and P.work(L) == P.work(run) / "live" / L["id"]
    assert [c["id"] for c in P.children(run["id"])] == [L["id"]]
    assert P.summary(P.load(L["id"]))["current"] == "live_run"
    assert "query" not in P.KINDS["run"] and P.KINDS["run"][-2:] == ["graph", "package"]


def test_a_live_run_is_queried_without_a_stage(tmp_path, call):
    run = integrated_app(tmp_path)
    L = P.new("live", "app-live", parent=run["id"])
    P.record(L["id"], "live_run", status="running", outputs={"traces": str(traces(tmp_path))})
    assert A.traces_of(P.load(L["id"])).endswith("raw")
    sm = call("query_summary", pipeline=L["id"])
    assert sm["entities_by_type"] == {"raw": 1, "clean": 1, "plot": 1} and sm["activities_by_name"] == {"prep": 1, "draw": 1}
    assert all(q.startswith("TraceViewer(") for q in sm["queries"])
    r = call("evidence_answer", pipeline=L["id"], question="Where did the plot come from?",
             steps=[{"op": "entities", "type": "plot"}, {"op": "lineage", "from": 0, "direction": "back"}, {"op": "ends", "from": 1}])
    assert r["status"] == "ok" and r["steps"][2]["result"].startswith("1 roots") and "1 raw" in r["steps"][2]["result"]
    s = call("evidence_synthesis", pipeline=L["id"], id=r["id"], text="The plot came from 1 raw input [S3], through 2 activities [S2].")
    assert s["synthesis_check"]["ok"], s
    rec = A.listing(L["id"])[-1]
    assert rec["engine"] == "traceviewer" and rec["synthesis"].startswith("The plot") and len(A.listing(L["id"])) == 1      # updated, not duplicated
    assert {n["id"] for n in rec["lineage"]["nodes"] if n["kind"] == "entity"} == {"p", "c", "r"}
    c = P.load(L["id"])
    assert [q["q"] for q in c["queries"]] == ["summary", "answer: Where did the plot come from?"] and c["stages"]["live_run"]["rev"] == 1


def test_the_graph_stage_records_the_diff_and_fails_on_a_promise_never_kept(tmp_path, call):
    run = integrated_app(tmp_path)
    P.record(run["id"], "baseline", outputs={"traces": str(traces(tmp_path))})
    h = call("graph_check", pipeline=run["id"])
    assert h["diff"]["clean"] and h["diff"]["summary"]["conditional"] == 1
    v = call("graph_validate", pipeline=run["id"])
    assert v["status"] == "pass", v
    spec = {**SPEC, "activities": SPEC["activities"] + [{"name": "save", "used": [{"type": "plot"}], "generated": [{"type": "archive"}]}]}
    P.record(run["id"], "annotate", outputs={"spec": spec})
    call("graph_check", pipeline=run["id"])
    v = call("graph_validate", pipeline=run["id"])
    assert v["status"] == "fail" and [f["name"] for f in v["failed"]] == ["expected_recorded"] and "archive" in v["failed"][0]["detail"]


def test_the_views_read_apps_live_runs_and_the_aggregated_graph(tmp_path):
    from moirai_agents.webui import workbench as W
    run = integrated_app(tmp_path)
    a = W.aggregate("", run["id"])
    assert a["mode"] == "expected" and {e["status"] for e in a["edges"]} == {"expected", "conditional"}      # nothing ran: what the spec promises
    P.record(run["id"], "baseline", outputs={"traces": str(traces(tmp_path))})
    for _ in range(20):
        a = W.aggregate("", run["id"])
        if not a.get("reading"):
            break
    assert a["mode"] == "diff" and a["clean"] and a["counts"]["entities"] == 3
    L = W.new_live("", run["id"], "", "python app.py --big", "chat1")
    items = W.items("")
    assert [x["id"] for x in items["apps"]] == [run["id"]] and items["apps"][0]["can_live"]
    lv = items["lives"][0]
    assert lv["id"] == L["id"] and lv["parent"] == run["id"] and lv["app"] == "app" and lv["chat"] == "chat1" and L["command"] == "python app.py --big"
    assert [s["name"] for s in lv["steps"]] == ["live_run", "publish_data"]
    P.update(run["id"], lambda c: c["stages"]["package"].update(status="needs_user"))
    assert W.items("")["apps"][0]["state"] == "needs_you"
    assert W.items("", {run["chat_session"] or "x"})["apps"][0]["state"] == ("working" if run["chat_session"] else "needs_you")
    P.update(run["id"], lambda c: c.update(chat_session="app-chat"))
    busy = W.items("", {"app-chat"})["apps"][0]          # its agent is working in its chat: not waiting for the user
    assert busy["state"] == "working" and busy["busy"]
    assert W.aggregate("", L["id"])["mode"] == "expected"          # the live run has not written yet: its parent's spec


def test_the_live_run_tools_in_the_project_layer(tmp_path, monkeypatch):
    """moirai_run and repro_package of the repo's capabilities/ (the layer the server loads) carry the live run's stages."""
    monkeypatch.setenv("MOIRAI_AGENTS_PROJECT_CAPABILITIES", str(REPO / "capabilities"))
    from moirai_agents.core import capstore
    capstore._State.loaded, capstore._State.fingerprint = {}, ""
    t = {}
    capstore.refresh(t, force=True)
    call = lambda _tool, **kw: t[_tool]["fn"](**kw)      # noqa: E731
    assert {"moirai_live_run", "repro_publish_data_validate", "graph_check"} <= set(t)
    run = integrated_app(tmp_path)
    L = P.new("live", "app-live", parent=run["id"])
    v = call("moirai_validate", pipeline=L["id"], stage="live_run")
    assert v["status"] == "fail" and v["failed"][0]["name"] == "ran"
    assert "publish_root and app" in call("repro_publish_traces", pipeline=L["id"])["error"]
    P.record(run["id"], "package", outputs={"publish_root": str(tmp_path), "publish_app": "app"})
    assert "no traces" in call("repro_publish_traces", pipeline=L["id"])["error"]
    v = call("repro_publish_data_validate", pipeline=L["id"])
    assert v["status"] == "fail" and v["failed"][0]["name"] == "published"


def test_live_inputs_detected_written_and_rendered(tmp_path, call):
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    for n in ("small.json", "big.json"):
        (app / "config" / n).write_text("{}")
    (app / "run.py").write_text("import argparse\np = argparse.ArgumentParser()\n"
                                "p.add_argument('--mode', choices=['fast', 'full'], default='fast', help='how thorough')\n"
                                "p.add_argument('--samples', type=int, default=5)\np.add_argument('--verbose', action='store_true')\n")
    run = P.new("run", "app", source=str(app), command="python run.py --config config/small.json")
    P.record(run["id"], "annotate", outputs={"spec": {"activities": [{"name": "x", "relations": [{"subject": {"key_expr": "resolve(kwargs, 'nstruct', env, 'NSTRUCT')"}}]}]}})
    d = {c["name"]: c for c in call("live_inputs_detect", pipeline=run["id"])["candidates"]}
    assert d["mode"]["kind"] == "choice" and d["mode"]["options"] == ["fast", "full"] and d["mode"]["default"] == "fast"
    assert d["samples"]["kind"] == "integer" and d["verbose"]["kind"] == "bool" and d["nstruct"]["env"] == "NSTRUCT"
    assert d["config_file"]["options"] == ["config/big.json", "config/small.json"] and d["config_file"]["default"] == "config/small.json"
    bad = call("live_inputs_write", pipeline=run["id"], command="python run.py --mode {mode} --n {n}", inputs=[{"name": "mode", "kind": "choice", "options": ["fast"], "default": "slow"}])
    assert bad["status"] == "error" and any("not inputs" in p for p in bad["problems"]) and any("default of mode" in p for p in bad["problems"])
    inputs = [{"name": "config", "label": "Target", "kind": "choice", "options": d["config_file"]["options"], "default": "config/small.json"},
              {"name": "mode", "kind": "choice", "options": ["fast", "full"], "default": "fast"}, {"name": "samples", "kind": "integer", "min": 1, "max": 50, "default": 5}]
    r = call("live_inputs_write", pipeline=run["id"], command="python run.py --config {config} --mode {mode} --samples {samples}", inputs=inputs)
    assert r == {"status": "ok", "inputs": 3, "example": {"status": "ok", "command": "python run.py --config config/small.json --mode fast --samples 5",
                                                         "values": {"config": "config/small.json", "mode": "fast", "samples": "5"}}}
    assert call("live_inputs_validate", pipeline=run["id"])["status"] == "pass"
    assert call("live_inputs_render", pipeline=run["id"], values={"config": "config/big.json", "samples": 80})["status"] == "error"
    assert call("live_inputs_render", pipeline=run["id"], values={"mode": "x; rm -rf /"})["status"] == "error"
    # the Live Run view: a live run made from chosen values gets the rendered command
    from moirai_agents.webui import workbench as W
    P.update(run["id"], lambda c: c["stages"]["integrate"].update(status="passed", outputs={"env": "/e", "annotated": str(app)}))
    L = W.new_live("", run["id"], "", "", "c1", {"config": "config/big.json", "mode": "full"})
    assert L["command"] == "python run.py --config config/big.json --mode full --samples 5" and L["inputs"]["mode"] == "full"
    with pytest.raises(P.PipelineError, match="not one of"):
        W.new_live("", run["id"], "", "", "c2", {"mode": "turbo"})


def test_a_background_helper_is_followed_from_its_transcript(tmp_path):
    from types import SimpleNamespace
    from moirai_agents.webui.runtime import Turn
    sub = tmp_path / "projects" / "-ws" / "S1" / "subagents"
    sub.mkdir(parents=True)
    lines = [{"type": "user", "isSidechain": True, "message": {"role": "user", "content": "do the graph"}},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__moirai__graph_check", "input": {"pipeline": "p"}}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"activities\": 3}"}]}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "The graph passes."}]}}]
    (sub / "agent-a1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (sub / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "AG", "requestShape": "background", "finished": True, "agentType": "prov_graph", "description": "graph"}))
    (sub / "agent-b2.meta.json").write_text(json.dumps({"toolUseId": "OTHER", "requestShape": "background", "finished": True}))   # not this turn's
    out = []
    fake = SimpleNamespace(claude_home=str(tmp_path), session={"claude_session": "S1"}, agent_uses={"AG"}, done=True, emit=out.append)
    Turn._tail_agents(fake)
    assert [(e["type"], e.get("parent")) for e in out] == [("tool_use", "AG"), ("tool_result", "AG"), ("text", "AG"), ("agent_done", None)]
    assert out[0]["name"] == "mcp__moirai__graph_check" and out[0]["background"] and out[-1]["id"] == "AG" and out[-1]["agent"] == "prov_graph"



def test_an_answer_is_computed_from_traceviewer_queries_with_its_trail(tmp_path):
    from moirai_agents.core import evidence as E
    run = integrated_app(tmp_path)
    tr = traces(tmp_path)
    P.record(run["id"], "baseline", outputs={"traces": str(tr)})
    with pytest.raises(E.PlanError, match="not in these traces"):
        E.ask(run["id"], "q", [{"op": "entities", "type": "nope"}])
    with pytest.raises(E.PlanError, match="earlier step"):
        E.ask(run["id"], "q", [{"op": "entities", "type": "raw"}, {"op": "lineage", "from": 1}])
    rec = E.ask(run["id"], "What did the raw data affect?", [{"op": "entities", "type": "raw"}, {"op": "lineage", "from": 0, "direction": "forward"},
                                                              {"op": "group", "from": 1, "by": "type"}], chose="you")
    assert rec["result"]["headline"] == "By type: clean 1, plot 1." and rec["plan"]["chosen_by"] == "you"
    assert rec["steps"][1]["headline"].startswith("raw r affected 2 downstream entities (1 clean, 1 plot) through 2 activities")
    assert rec["steps"][2]["computed_from"] == 1 and rec["steps"][2]["queries"] is None          # a count is computed, not queried
    assert all(q["query"].startswith('TraceViewer(<file>).phase("any")') for q in rec["queries"]) and rec["reads"][0]["file"] == "a.pfw.gz"
    assert {c["rec"] for c in rec["steps"][1]["cites"]} >= {"EH", "prep", "draw"}
    # time frames resolve to trace timestamps (microseconds), relative to the run too
    r = E.run(str(tr), [{"op": "activities", "since": "start+0us", "until": "start+1us"}], E.index_dir_of(run["id"]))
    assert r["outs"][0]["ids"] == ["1:1"] and r["steps"][0]["resolved"]["time"][0] != "-"
    r = E.run(str(tr), [{"op": "entities", "type": "plot", "produced_since": 2, "produced_until": 3}], E.index_dir_of(run["id"]))   # epoch µs given as 2..3 -> seconds
    assert r["outs"][0]["ids"] == []
    # the write-up is checked against the result
    bad = E.synthesize(run["id"], rec["id"], "It affected 5 entities [S7].")
    assert not bad["synthesis_check"]["ok"] and any("S7" in p for p in bad["synthesis_check"]["problems"]) and any("5" in p for p in bad["synthesis_check"]["problems"])
    # re-run: same traces, same result; a changed trace says so
    assert E.rerun(run["id"], rec["id"])["same"]
    (tr / "b.pfw.gz").write_bytes(member(eh("q", "plot"), act("draw", 9, used=["c"], generated=["q"])))
    rr = E.rerun(run["id"], rec["id"])
    assert not rr["same"] and rr["headline"] == "By type: plot 2, clean 1."
