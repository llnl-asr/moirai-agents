"""The three views' backend: live pipelines (a long run under an annotated app), the aggregated entity-relationship graph
and its diff against the spec (the Annotate view's check), and the endpoints the views read."""
import gzip
import json
import os
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
    assert d["clean"] and d["unexpected_types"] == ["log"] and d["summary"] == {"matched": 4, "missing": 0, "conditional": 1, "unexpected": 1, "captured": 0}
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


def test_the_annotation_helper_captures_inputs_outputs_and_attributes(tmp_path):
    """The generated _moirai_prov.py records an activity's parameters (arguments with defaults, the object's attributes, command-line flags,
    named values), its return value, and an entity's attributes, each as its own name=value entity."""
    import importlib.util
    import subprocess
    import sys
    spec = importlib.util.spec_from_file_location("prov_annotate_tool", REPO / "capabilities/prov_annotate/tool.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake/moirai.py").write_text(t.FAKE_MOIRAI)
    (tmp_path / "_moirai_prov.py").write_text(t.HELPER % json.dumps([["filtered_msa", "OUTPUT", "filtered"]]))
    (tmp_path / "app.py").write_text('''
import _moirai_prov
from pathlib import Path
class Task:
    def __init__(self, run_str):
        self.run_str, self.config = run_str, {"retries": 2}
    @_moirai_prov.activity("filter_msa", generated=[("filtered_msa", lambda self=None, **_: "k", "LOCAL_DISK", "/o.a3m", None, {"n_lines": lambda self=None, **_: 42})],
                           capture={"inputs": {"command": lambda self=None, **_: self.run_str}})
    def on_finish(self, threshold=0.5):
        return {"kept": 1200}
    @_moirai_prov.activity("quiet", capture={"auto": False, "output": False})
    def quiet(self, x=1):
        return 5
t = Task("mmseqs filtera3m in out --qid 0.0,0.2 --diff 3000 -v 1")
t.on_finish(); t.quiet()
_moirai_prov.stop()
''')
    env = {**os.environ, "MOIRAI_FAKE_OUT": str(tmp_path / "out.json"), "PYTHONPATH": f"{tmp_path / 'fake'}:{tmp_path}"}
    subprocess.run([sys.executable, "app.py"], cwd=tmp_path, env=env, check=True)
    out = json.loads((tmp_path / "out.json").read_text())
    ev = {e["activity"]: e["relations"] for e in out["events"]}
    cap = out["captured"]                                                      # the fake keeps captured values apart from the promised relations
    assert ev["filter_msa"] == [["GENERATED", "filtered_msa", "k"]] and ev["quiet"] == []
    assert not [c for c in cap if c.get("activity") == "quiet"]                # capture off: nothing extra
    inputs = {c["value"] for c in cap if c.get("type") == "filter_msa.input"}
    assert {"--qid=0.0,0.2", "--diff=3000", "-v=1", "threshold=0.5"} <= inputs and any(v.startswith("command=mmseqs") for v in inputs)
    assert [c["value"] for c in cap if c.get("type") == "filter_msa.output"] == ["value.kept=1200"]
    assert [c for c in cap if c.get("type") == "filtered_msa.attribute"] == [{"of": ["filtered_msa", "k"], "type": "filtered_msa.attribute", "value": "n_lines=42"}]
    assert not [t for t in out["types"] if t.endswith((".input", ".output", ".attribute"))]


def test_captured_inputs_outputs_and_attributes_are_queried(tmp_path):
    from moirai_agents.core import evidence as E
    d = tmp_path / "traces" / "raw"
    d.mkdir(parents=True)
    rows = [et("msa", 1), et("filtered", 3), et("filter_msa.input", 4), et("filter_msa.output", 3), et("filtered.attribute", 5), eh("m", "msa"), eh("f", "filtered"),
            {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": "i1", "type": "filter_msa.input", "uri": "--qid=0.0,0.2"}},
            {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": "i2", "type": "filter_msa.input", "uri": "threshold=0.5"}},
            {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": "i3", "type": "filter_msa.input", "uri": "--qid=0.4"}},
            {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": "o1", "type": "filter_msa.output", "uri": "value.kept=1200"}},
            {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": "a1", "type": "filtered.attribute", "uri": "n_lines=42"}},
            {"name": "ER", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"subject": "a1", "object": "f", "relation": 1}},
            act("filter_msa", 1, used=["m", "i1", "i2"], generated=["f", "o1"]), act("filter_msa", 2, used=["m", "i3", "i2"], generated=["f"])]
    (d / "a.pfw.gz").write_bytes(member(*rows))
    r = E.run(str(d), [{"op": "inputs", "activity": "filter_msa"}], str(tmp_path / "idx"))
    assert r["steps"][0]["headline"] == "filter_msa ran 2 time(s); its parameters: --qid = 0.0,0.2 | 0.4; threshold = 0.5."
    assert "| --qid | 0.0,0.2 \\| 0.4 | 2 |" in r["steps"][0]["table"]
    assert E.run(str(d), [{"op": "outputs", "activity": "filter_msa"}], str(tmp_path / "idx"))["steps"][0]["headline"].endswith("value.kept = 1200.")
    r = E.run(str(d), [{"op": "entities", "type": "filtered"}, {"op": "attributes", "from": 0}], str(tmp_path / "idx"))
    assert r["steps"][1]["headline"] == "attributes of 1 entity: n_lines = 42."
    r = E.run(str(d), [{"op": "inputs", "activity": "nope"}], str(tmp_path / "idx"))      # not a plan error: a gap the user can fix
    assert [(g["kind"], g["subject"]) for g in r["gaps"]] == [("not_recorded", "nope")]


def test_captured_inputs_answer_and_missing_ones_are_gaps(tmp_path, call):
    from moirai_agents.core import evidence as E
    run = integrated_app(tmp_path)
    d = tmp_path / "traces" / "raw"
    d.mkdir(parents=True)
    rec_in = lambda i, uri: {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": i, "type": "draw.input", "uri": uri, "store": 0}}   # noqa: E731
    (d / "a.pfw.gz").write_bytes(member(et("raw", 1), et("clean", 3), et("plot", 2), et("draw.input", 4), eh("r", "raw"), eh("c", "clean"), eh("p", "plot"),
                                        rec_in("i1", "--qid=0.2"), rec_in("i2", "--diff=3000"),
                                        act("prep", 1, used=["r"], generated=["c"]), act("draw", 2, used=["c", "i1", "i2"], generated=["p"])))
    P.record(run["id"], "baseline", outputs={"traces": str(d)})
    rec = E.ask(run["id"], "What are the draw parameters?", [{"op": "inputs", "activity": "draw"}])
    assert rec["result"]["headline"] == "draw ran 1 time(s); its parameters: --qid = 0.2; --diff = 3000."
    assert "| --qid | 0.2 | 1 |" in rec["result"]["table"] and not rec["gaps"]
    lin = E.ask(run["id"], "Where did the plot come from?", [{"op": "entities", "type": "plot"}, {"op": "lineage", "from": 0, "direction": "back"}])
    gaps = {(g["kind"], g["subject"]) for g in lin["gaps"]}
    assert gaps == {("no_inputs", "prep")}                      # draw recorded its inputs; prep did not; the input entities are not roots to fix
    none = E.ask(run["id"], "What are the filter parameters?", [{"op": "inputs", "activity": "filter_msa"}])
    assert [(g["kind"], g["subject"]) for g in none["gaps"]] == [("not_recorded", "filter_msa")] and "none recorded" in none["result"]["headline"]


def test_a_gap_is_opened_on_the_app_and_closed_only_with_what_was_learned(tmp_path, monkeypatch):
    from moirai_agents.core import evidence as E, gaps as G
    run = integrated_app(tmp_path)
    P.update(run["id"], lambda c: c.update(chat_session="app-chat"))
    P.record(run["id"], "baseline", outputs={"traces": str(traces(tmp_path))})
    L = P.new("live", "app-live", parent=run["id"])
    P.record(L["id"], "live_run", outputs={"traces": P.stage(P.load(run["id"]), "baseline")["outputs"]["traces"]})
    rec = E.ask(L["id"], "What happened in prep?", [{"op": "activities", "name": "prep"}])
    g = rec["gaps"][0]
    assert g["kind"] == "no_inputs" and g["subject"] == "prep"
    o = G.open_gap(L["id"], rec["id"], g["id"])                  # asked on the live run: kept on its app, to fix in the annotation
    assert o["app"] == run["id"] and o["chat"] == "app-chat" and o["status"] == "open"
    assert G.open_gap(L["id"], rec["id"], g["id"])["id"] == o["id"] and len(G.listing(run["id"])) == 1       # once
    bad = G.close(run["id"], o["id"], "guessed", {}, {"capability": "prov_query", "version": 1}, "short")
    assert bad["status"] == "error" and any("cause" in p for p in bad["problems"])
    bad = G.close(run["id"], o["id"], "missed_inputs", {"capability": "prov_annotate", "version": 999}, {"capability": "prov_query", "version": 1}, "x" * 40)
    probs = " ".join(bad["problems"])
    assert "not v999" in probs and "workload_<app>" in probs
    wl = tmp_path / "pcaps" / "workload_app"
    wl.mkdir(parents=True)
    (wl / "capability.json").write_text(json.dumps({"name": "workload_app", "version": 2, "enabled": True, "tools": [], "description": "the app"}))
    monkeypatch.setenv("MOIRAI_AGENTS_PROJECT_CAPABILITIES", str(tmp_path / "pcaps"))
    from moirai_agents.core import capstore
    pa = capstore.all_caps()["prov_annotate"][2]["version"]
    how = "prep reads its options from self.config, not its arguments: captured with capture.inputs"
    r = G.close(run["id"], o["id"], "missed_inputs", {"capability": "prov_annotate", "version": pa}, {"capability": "workload_app", "version": 2}, how)
    assert r == {"status": "ok", "gap": o["id"], "state": "fixed_pending_run"}
    again = E.ask(run["id"], "What happened in prep?", [{"op": "activities", "name": "prep"}])      # still the old traces: the gap is still there
    assert G.close(run["id"], o["id"], "missed_inputs", {"capability": "prov_annotate", "version": pa}, {"capability": "workload_app", "version": 2}, how,
                   recheck_answer=again["id"])["status"] == "error"


def test_the_gaps_of_every_query_are_collected_onto_the_app_once(tmp_path):
    """gap_collect: every answer on the app and its live runs, one gap per kind and subject; what the write-up says is not recorded is a gap;
    a gap fixed after the answer that showed it is not reopened."""
    from moirai_agents.core import answers as A, evidence as E, gaps as G
    run = integrated_app(tmp_path)
    P.update(run["id"], lambda c: c.update(chat_session="app-chat"))
    P.record(run["id"], "baseline", outputs={"traces": str(traces(tmp_path))})
    L = P.new("live", "app-live", parent=run["id"])
    P.record(L["id"], "live_run", outputs={"traces": P.stage(P.load(run["id"]), "baseline")["outputs"]["traces"]})
    E.ask(run["id"], "What happened in prep?", [{"op": "activities", "name": "prep"}])
    E.ask(L["id"], "What happened in prep, live?", [{"op": "activities", "name": "prep"}])
    said = E.ask(L["id"], "Which prep run failed?", [{"op": "activities", "name": "prep"}],
                 synthesis="prep ran [S1]. No exit status is recorded for prep, so the traces do not say which run failed.")
    assert [g["kind"] for g in A.listing(L["id"])[-1]["gaps"]] == ["no_inputs"]       # a structural gap found: the write-up adds none
    assert E.reported_gaps(said["synthesis"], 1, [])[0]["kind"] == "reported"
    assert set(G.related(run["id"])) == {run["id"], L["id"]} and G.related(L["id"]) == []
    r = G.collect(L["id"])                                        # from the live run: kept on its app
    assert r["app"] == run["id"] and r["chat"] == "app-chat" and r["answers"] == 3 and r["added"] == 1
    (g,) = r["open"]
    assert (g["kind"], g["subject"]) == ("no_inputs", "prep") and len(g["seen_in"]) == 2      # three answers, one gap
    again = G.collect(run["id"])
    assert again["added"] == 0 and len(again["open"][0]["seen_in"]) == 2                     # collecting twice changes nothing
    P.update(run["id"], lambda c: c["gaps"][0].update(status="fixed_pending_run", closed=__import__("time").time() + 1))
    assert G.collect(run["id"])["open"] == []                    # fixed after these answers: not reopened by them



def test_captured_values_are_properties_of_the_nodes_they_describe(tmp_path):
    """Aggregated: an entity type lists its attribute names, an activity its input names. Instances: the actual values. No captured-value nodes."""
    from moirai_agents.core import aggregate as G
    from moirai_agents.webui import workbench as W
    d = tmp_path / "raw"
    d.mkdir()
    cap = lambda i, t, uri: {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": i, "type": t, "uri": uri, "store": 0}}     # noqa: E731
    (d / "a.pfw.gz").write_bytes(member(et("msa", 1), et("filtered", 3), eh("m", "msa"), eh("f", "filtered"), cap("i1", "filter.input", "--qid=0.2"),
                                        cap("a1", "filtered.attribute", "n_seqs=812"),
                                        {"name": "ER", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"subject": "a1", "object": "f", "relation": 19}},
                                        act("filter", 1, used=["m", "i1"], generated=["f"])))
    g = T.read(str(d))
    agg = {n["id"]: n for n in G.runtime(g)["nodes"]}
    assert set(agg) == {"t:msa", "t:filtered", "a:filter"}
    assert agg["t:filtered"]["attrs"] == ["n_seqs"] and agg["a:filter"]["inputs"] == ["--qid"]
    inst = {n["id"]: n for n in W._nodes(g)["nodes"]}
    assert set(inst) == {"m", "f", "1:1"} and inst["f"]["attrs"] == ["n_seqs=812"] and inst["1:1"]["inputs"] == ["--qid=0.2"]
    exp = G.expected({"entity_types": [{"type": "filtered", "role": "output"}], "activities": [{"name": "filter", "generated": [{"type": "filtered", "attributes": {"n_seqs": "len(x)", "depth": "d"}}],
                                                                                              "capture": {"inputs": {"command": "self.run_str"}}}]})
    diff = {n["id"]: n for n in G.diff(exp, G.runtime(g))["nodes"]}
    assert diff["t:filtered"]["attrs"] == ["depth", "n_seqs"] and diff["a:filter"]["inputs"] == ["--qid", "command"]


def test_running_a_stage_again_resets_the_stages_built_on_it(tmp_path):
    run = integrated_app(tmp_path)
    for st in ("moirai_smoke", "baseline", "graph", "package"):
        P.update(run["id"], lambda c, st=st: c["stages"][st].update(status="passed", validation={"status": "pass"}, gate={"approved": True}))
    P.record(run["id"], "baseline", status="running", note="re-run after re-annotating")
    c = P.load(run["id"])
    assert [c["stages"][s]["status"] for s in ("moirai_smoke", "baseline", "graph", "package")] == ["passed", "running", "pending", "pending"]
    assert c["stages"]["graph"]["validation"] is None and c["stages"]["package"]["gate"] is None
    assert "baseline is running again" in c["stages"]["graph"]["history"][-1]["note"]


def test_an_explanation_reads_tables_with_several_values_and_keeps_values_exact():
    from moirai_agents.core import explain as X
    t = X.md_table("| name | value(s) | runs |\n|---|---|---|\n| --qid | 0.0 \\| 0.5 \\| 1.0 | 3 |\n| --maxseq | 1000000 | 3 |")
    assert t["columns"] == ["name", "value(s)", "runs"] and all(len(r) == 3 for r in t["rows"])
    assert X.split_values(t["rows"][0][1]) == ["0.0", "0.5", "1.0"] and t["rows"][1][1] == "1000000"   # parameters as passed
    assert X.fmt_value("1794203908.2100208").endswith("UTC") and 2 in t["numeric"]
    old = X.md_table("| name | value(s) | runs |\n|---|---|---|\n| -i | /a | /b | 10 |")       # unescaped pipes (older answers)
    assert old["rows"][0] == ["-i", "/a | /b", "10"]


def test_query_explain_gives_four_parts_summary_first_and_passes_its_checks():
    from moirai_agents.core import capstore as S
    fix = json.loads((Path(S.__file__).parent.parent / ".agents/capabilities/query_explain/checks.json").read_text())[0]["args"]["answer"]
    r = S.call("query_explain", "query_explain", answer=fix)
    assert [p["part"] for p in r["parts"]] == ["plan", "result", "answer", "evidence"] and not r["problems"]
    assert all(p["summary"] and len(p["summary"]) <= 320 for p in r["parts"])
    res = r["parts"][1]
    assert "differ between runs" in res["summary"] and any(b["type"] == "table" for d in res["details"] for b in d["blocks"])
    assert S.call("query_explain", "query_explain_validate", answer=fix)["status"] == "pass"


def test_a_stage_the_kind_no_longer_has_is_retired_not_left_running(tmp_path):
    run = P.new("run", "old", source=str(tmp_path), command="python app.py")
    P.update(run["id"], lambda c: c["stages"].update(query={"name": "query", "status": "running"}) or {"kind": "test"})
    s = P.load(run["id"])["stages"]["query"]
    assert s["status"] == "skipped" and s["retired"]


def test_an_answer_without_a_lineage_step_carries_the_subgraph_it_used():
    from moirai_agents.core import evidence as EV
    ctx = {"acts": {"1:10": {"id": "1:10", "name": "f", "used": ["e1", "p1"], "generated": ["e2"]}}}
    o = EV._touched([{"kind": "inputs", "runs": [{"activity": "1:10"}], "ids": []}], ctx)
    assert o["activities"] == ["1:10"] and set(o["entities"]) == {"e1", "p1", "e2"}
    assert EV._touched([{"kind": "group", "counts": {}}], {}) is None


def test_every_part_is_checked_against_the_traces_and_the_confidence_counts_what_was_calculated(tmp_path):
    from moirai_agents.core import capstore as S, evidence as E
    run = integrated_app(tmp_path)
    d = tmp_path / "traces" / "raw"
    d.mkdir(parents=True)
    rec_in = lambda i, uri: {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": i, "type": "draw.input", "uri": uri, "store": 0}}   # noqa: E731
    (d / "a.pfw.gz").write_bytes(member(et("clean", 3), et("plot", 2), et("draw.input", 4), eh("c", "clean"), eh("p", "plot"),
                                        rec_in("i1", "--qid=0.2"), rec_in("i2", "--diff=3000"), rec_in("i3", "--qid=0.4"),
                                        act("draw", 2, used=["c", "i1", "i2"], generated=["p"]), act("draw", 3, used=["c", "i3", "i2"], generated=["p"])))
    P.record(run["id"], "baseline", outputs={"traces": str(d)})
    rec = E.ask(run["id"], "What are the draw parameters?", [{"op": "inputs", "activity": "draw"}])
    v = S.call("query_explain", "query_explain_validate", pipeline=run["id"], answer_id=rec["id"])
    assert v["status"] == "pass", v
    assert all(p["data"].startswith("checked against the traces") for p in v["parts"].values())
    ans = S.call("explain_answer", "explain_answer", answer=rec)
    assert "[R" in ans["summary"] + " ".join(b.get("text", "") for b in ans["body"]) and "No write-up" not in ans["summary"]
    lie = {**rec, "result": {**rec["result"], "table": rec["result"]["table"].replace("3000", "4000")}}     # a value the traces do not have
    bad = S.call("explain_answer", "explain_answer_check", answer=lie, pipeline=run["id"])
    assert bad["status"] == "fail" and any("4000" in x for x in bad["problems"])
    c = S.call("answer_confidence", "confidence_score", pipeline=run["id"], answer_id=rec["id"])
    assert c["score"] == 100 and c["stale"] is False and c["llm_inferred"] == 0
    guess = {**rec, "synthesis": "The draw used qid 0.2 and 0.4 [S1].\nThese come from the trace.\nIt converges 2.5 times faster with 0.4."}
    g = S.call("answer_confidence", "confidence_score", answer=guess)
    assert [i["how"] for i in g["items"] if i.get("llm")] == ["llm_verified", "wording", "llm"] and g["llm_inferred"] == 1
    assert "2.5" in [i for i in g["items"] if i["how"] == "llm"][0]["why"] and g["score"] == 75      # 3 of the 4 parts with values come from queries; wording is not counted
    assert S.call("answer_confidence", "confidence_validate", answer=guess)["status"] == "pass"


def _publish(root, name, ts, kind):
    d = root / "provenance-traces" / name / ts
    (d / "raw").mkdir(parents=True)
    with gzip.open(d / "raw" / "prov.pfw.gz", "wt") as fh:
        fh.write(json.dumps({"name": "EH", "cat": "dftracer", "ph": 4, "args": {"id": f"e-{name}-{ts}", "type": "t"}}) + "\n")
    (d / "run.json").write_text(json.dumps({"app": {"name": name, "source_commit": "abc"}, "run": {"kind": kind, "scale": "baseline", "timestamp": ts}}))
    (root / "provenance-repro" / name).mkdir(parents=True, exist_ok=True)


def test_every_published_version_of_an_app_can_be_asked(tmp_path):
    from moirai_agents.webui import workbench as W
    root = tmp_path / "pub"
    _publish(root, "fold", "20261030_100000", "baseline")
    _publish(root, "fold", "20261031_100000", "verification")
    _publish(root, "fold-v2", "20261108_100000", "baseline")
    _publish(root, "fold-v4", "20261108_100000", "baseline")           # the same run published again
    (root / "provenance-repro" / "fold-v3").mkdir(parents=True)          # a package without traces
    _publish(root, "fold-other", "20261108_100000", "baseline")        # another app
    app = P.new("run", "fold", source=str(tmp_path), command="python app.py")
    P.update(app["id"], lambda c: c["stages"]["package"]["outputs"].update(publish_root=str(root), publish_app="fold-v4"))
    W._PUB.clear()
    runs = W.published_runs(P.load(app["id"]))
    assert [(r["label"], r["kind"], r["ts"]) for r in runs] == [("v1", "baseline", "20261030_100000"), ("v2", "baseline", "20261108_100000"),
                                                                ("v4", "baseline", "20261108_100000")]      # v3 has no traces
    assert runs[2]["same_as"] == "v2" and "fold-other" not in str(runs)
    d = W.open_published("", app["id"], runs[1]["path"], "chat1")
    assert d["kind"] == "analysis" and d["parent"] == app["id"] and d["published"]["label"] == "v2"
    assert W.open_published("", app["id"], runs[1]["path"], "chat2")["id"] == d["id"]          # made once
    with pytest.raises(P.PipelineError):
        W.open_published("", app["id"], str(tmp_path), "chat3")                                # only the app's own published runs
    it = W.items("")
    assert [x["id"] for x in it["published"]] == [d["id"]] and it["datasets"] == []
    a = next(x for x in it["apps"] if x["id"] == app["id"])
    assert next(r for r in a["versions"] if r["label"] == "v2")["pipeline"] == d["id"]
    assert A.graph(P.load(d["id"]))[0].entities                                                # its traces are what is asked


def test_a_how_good_question_is_answered_directly_with_a_verdict_and_each_items_values(tmp_path):
    """'What is the quality of the MSAs?' opens with the verdict and the numbers over every item, then each MSA's own values best first,
    joined with its template search by folder - and all of it holds against the traces."""
    from moirai_agents.core import capstore as S, evidence as E
    run = integrated_app(tmp_path)
    d = tmp_path / "traces" / "raw"
    d.mkdir(parents=True)
    recs = [et("msa", 3), et("hits", 3), et("msa.attribute", 1), et("hits.attribute", 1)]
    for k, (prob, cov) in enumerate([(98.6, 0.79), (82.0, 0.71), (54.3, 0.12), (22.3, 0.65), (17.3, 0.70)]):
        recs += [{"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": f"m{k}", "type": "msa", "uri": f"/al/Q{k}/a.a3m"}},
                 {"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": f"h{k}", "type": "hits", "uri": f"/al/Q{k}/h.hhr"}}]
        for i, t, uri, obj in ((f"am{k}", "msa.attribute", f"mean_query_coverage={cov}", f"m{k}"), (f"ah{k}", "hits.attribute", f"best_hit_probability={prob}", f"h{k}")):
            recs += [{"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"id": i, "type": t, "uri": uri, "store": 0}},
                     {"name": "ER", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"subject": i, "object": obj, "relation": 19}}]
    (d / "a.pfw.gz").write_bytes(member(*recs))
    P.record(run["id"], "baseline", outputs={"traces": str(d)})
    rec = E.ask(run["id"], "What is the quality of the MSAs?", [{"op": "entities", "type": "msa", "all": True}, {"op": "attributes", "from": 0},
                                                              {"op": "entities", "type": "hits", "all": True}, {"op": "attributes", "from": 2}])
    t = rec["steps"][3]["table"]
    assert "| median |" in t and "| 54.3 |" in t and rec["steps"][3]["per"].count("Q") == 5
    ans = S.call("explain_answer", "explain_answer", answer=rec)
    assert ans["summary"].startswith("The quality of the MSAs is mixed: `best_hit_probability` has a median of `54.3`, from `17.3` to `98.6` [R1]; 3 of 5")
    each = next(b for b in ans["body"] if b["type"] == "table")
    assert each["columns"] == ["folder", "best_hit_probability", "mean_query_coverage"]
    assert [r[0] for r in each["rows"]] == ["Q0", "Q1", "Q2", "Q3", "Q4"] and each["rows"][2] == ["Q2", "54.3", "0.12"]
    v = S.call("explain_answer", "explain_answer_check", pipeline=run["id"], answer_id=rec["id"])
    assert v["status"] == "pass", v


def test_queries_are_kept_with_their_trace_and_seen_from_every_pipeline_on_it(tmp_path, monkeypatch):
    """Every computed answer is kept in the query store under its trace, one file per query in the asker's folder; a dataset on the same
    traces lists it with who asked and its category; the write-up updates the asker's file; derived fields are never stored."""
    from moirai_agents.core import evidence as E, querystore as QS
    run = integrated_app(tmp_path)
    tr = str(traces(tmp_path))
    P.record(run["id"], "baseline", outputs={"traces": tr})
    monkeypatch.setenv("MOIRAI_AGENTS_USER", "alice")
    rec = E.ask(run["id"], "What happened in prep?", [{"op": "activities", "name": "prep"}])
    key = QS.trace_key(tr)
    assert key.startswith("local/") and rec["asked_by"] == "alice"
    files = sorted((QS.root() / key / "queries" / "alice").glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith(f"-{rec['id']}.json") and (QS.root() / key / "trace.json").is_file()
    doc = json.loads(files[0].read_text())
    assert doc["schema"] == QS.SCHEMA and doc["asked_by"] == "alice" and doc["category"] == "Traceability"
    assert doc["record"]["id"] == rec["id"] and doc["steps"] and doc["queries"] and doc["gaps"] == rec["gaps"]
    monkeypatch.setenv("MOIRAI_AGENTS_USER", "bob")                 # someone else, same second: their own file, no conflict
    rec2 = E.ask(run["id"], "Where did the plot come from?", [{"op": "entities", "type": "plot"}, {"op": "lineage", "from": 0, "direction": "back"}])
    assert len(list((QS.root() / key / "queries" / "bob").glob("*.json"))) == 1
    ds = P.new("analysis", "dataset-on-same-traces", traces=tr)      # another pipeline on the same traces sees both
    seen = {a["id"]: a for a in A.listing(ds["id"])}
    assert set(seen) >= {rec["id"], rec2["id"]} and seen[rec["id"]]["asked_by"] == "alice" and seen[rec2["id"]]["category"] == "Traceability"
    assert seen[rec["id"]].get("stored_file")
    E.synthesize(run["id"], rec["id"], "prep ran [S1]; the traces do not say why.")     # the write-up lands in the asker's file
    doc = json.loads(files[0].read_text())
    assert doc["answer"]["llm_response"].startswith("prep ran") and "category" not in doc["record"] and "stored_file" not in doc["record"]
    assert not [g for g in doc["record"]["gaps"] if g.get("kind") == "reported"]
    assert QS.trace_key("/p/vast1/moirai/provenance-traces/elmerfold-v10/20261111_154439/raw") == "elmerfold-v10/20261111_154439"
    gid = QS.group_id()                                               # every artifact group-shared: folders setgid rwxrws---, files rw-rw----
    for p in [QS.root() / key, QS.root() / key / "queries", QS.root() / key / "queries" / "alice", QS.root() / key / "trace.json", files[0]]:
        st = p.stat()
        assert (st.st_mode & 0o7777) == (QS.DIR_MODE if p.is_dir() else QS.FILE_MODE), (p, oct(st.st_mode))
        assert gid is None or st.st_gid == gid, (p, st.st_gid, gid)
    assert QS.fix_permissions(key) == {"fixed": 0, "others": []}


def test_a_new_app_starts_blank_with_a_chat_per_view(tmp_path, call, monkeypatch):
    from moirai_agents.webui import prompts
    from moirai_agents.webui import workbench as W
    with pytest.raises(P.PipelineError):
        P.new("live", "x", draft=True)                     # only an app can be a draft
    a = W.new_app("", "my app!", "cann", "clive", "cquery")
    assert a["name"] == "my-app" and a["draft"] and not a["source"] and not a["command"]
    row = W.items("")["apps"][0]
    assert row["draft"] and row["chat"] == "cann" and row["live_chat"] == "clive" and row["query_chat"] == "cquery" and not row["can_live"]
    assert "pipeline_new(kind='run', name='my-app'" in prompts.system_prompt("run", {"id": "cann", "app": a["id"], "role": "annotate"})
    assert "has not yet" in prompts.system_prompt("run", {"id": "clive", "app": a["id"], "role": "live"})
    monkeypatch.setenv("MOIRAI_AGENTS_WEBUI_SESSION", "cann")      # the agent in the Annotate chat fills the draft, it makes no second app
    r = call("pipeline_new", kind="run", name="other", source=str(tmp_path), command="python app.py")
    assert r["id"] == a["id"] and not r["draft"] and len(P.list_all()) == 1
    assert P.load(a["id"])["command"] == "python app.py" and "Start with pipeline_status" in prompts.system_prompt("run", {"app": a["id"], "role": "annotate"})
    with pytest.raises(P.PipelineError):
        P.fill_draft(a["id"], str(tmp_path), "again")       # no longer a draft
    P.update(a["id"], lambda c: c["stages"]["integrate"].update(status="passed", outputs={"env": "/e/moirai_env.sh"}))
    assert W.unused_live_chat("", a["id"]) == "clive"       # the first live run takes the blank Live Run chat
    L = W.new_live("", a["id"], "", "python app.py --big", "clive")
    assert W.unused_live_chat("", a["id"]) == ""
    assert f"drives live run {L['id']}" in prompts.system_prompt("run", {"id": "clive", "app": a["id"], "role": "live"})


def test_the_new_app_endpoint_makes_the_app_and_its_three_chats(web, tmp_path):
    w = web(MOIRAI_AGENTS_USER_WORKSPACE=str(tmp_path / "ws"))      # the server writes pipelines where it reads them
    r = w.post("/api/workbench/newapp", {"name": "demo"})
    ids = r["chats"]
    assert r["name"] == "demo" and len(set(ids.values())) == 3
    chats = {k: w.get(f"/api/session/{v}") for k, v in ids.items()}
    assert [chats[k]["turns"] for k in ids] == [[], [], []]
    assert chats["ann"]["mode"] == chats["live"]["mode"] == "run" and chats["query"]["mode"] == "ask"
    assert chats["ann"]["app"] == chats["live"]["app"] == chats["query"]["pipeline"] == r["pipeline"]
    app = w.get("/api/workbench")["apps"][0]
    assert app["id"] == r["pipeline"] and app["draft"] and app["chat"] == ids["ann"] and app["live_chat"] == ids["live"]
    assert w.post("/api/workbench/querychat", {"pipeline": r["pipeline"]})["session"] == ids["query"]      # the Query view finds its chat
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):                 # 400: an app needs a name
        w.post("/api/workbench/newapp", {"name": "  "})



@pytest.mark.parametrize("aspect", ["traceability", "accountability", "decision", "performance"])
def test_each_aspect_of_provenance_answers_its_questions_from_the_trace(aspect):
    """Each aspect's summary on a synthetic trace with every aspect in it states what the trace holds (through TraceViewer only)."""
    from moirai_agents.core import aspects as AS
    r = AS.selftest(aspect)
    assert r["status"] == "ok", r


def test_identities_are_hashes_and_an_anonymized_trace_names_no_one(tmp_path):
    from moirai_agents.core import evidence as E, querystore as QS
    from moirai_agents.core.aspects import _selftest as ST
    d = ST.write(tmp_path / "named")
    assert E.run(d, [{"op": "context"}], d)["steps"][0]["headline"].startswith("Run by alice (group lab), job f1234")
    a = ST.write(tmp_path / "anon", anonymized=True)
    head = E.run(a, [{"op": "context"}], a)["steps"][0]["headline"]
    assert "alice" not in head and "lab" not in head and f"anonymized user {ST.UH}" in head and f"anonymized group {ST.GH}" in head
    assert QS.category({"plan": {"steps": [{"op": "entities"}, {"op": "perf_io"}]}}) == "Performance"
    assert QS.category({"plan": {"steps": [{"op": "context"}]}}) == "Accountability"
    assert QS.category({"plan": {"steps": [{"op": "status"}]}}) == "Decision making"
