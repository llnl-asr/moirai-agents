"""Answer provenance questions from the traces, through dftracer-utils' TraceViewer, with the evidence (core.evidence): a question is resolved
into a plan of concrete steps (entities, activities, time frames, lineage), each step is a TraceViewer query, the result is computed, and the
agent's write-up must cite the steps. The analysis pipeline's query stage, and the Query view of a run (no stage there: questions are logged on
the pipeline)."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from moirai_agents.core import moirai_env as M
from moirai_agents.core import pipeline as P
from moirai_agents.core import answers as A
from moirai_agents.core import evidence as E

STAGE = "query"

PREAMBLE = '''
import os
from dftracer.utils import DataFrame, TraceViewer, col
TRACES, INDEX = os.environ["MOIRAI_Q_TRACES"], os.environ["MOIRAI_Q_INDEX"]
tv = TraceViewer(TRACES, INDEX)
# provenance records: activities plus the ET, EH and ER metadata records (phase "any" includes metadata)
graph = tv.phase("any").filter(col("cat").is_in(["moirai", "PROV"]) | col("name").is_in(["EH", "ET", "ER"])).collect()
acts = graph.filter(col("cat").is_in(["moirai", "PROV"]))
'''


def _traces(c):
    return A.traces_of(c)


def _log(pipeline, what, result):
    def fn(c):
        q = {"q": what[:300], "result": str(result)[:300], "ts": round(time.time(), 1)}
        if STAGE in P.KINDS.get(c["kind"], []):      # the analysis pipeline's query stage
            s = c["stages"][STAGE]
            s["status"] = "running"
            s["outputs"].setdefault("queries", []).append(q)
            s["rev"] += 1
        else:                                         # a run: asked from the Query view, kept on the pipeline (no stage, no verdict to invalidate)
            c["queries"] = (c.get("queries") or [])[-499:] + [q]
        return {"kind": "query", "q": what[:200]}
    P.update(pipeline, fn)


def _run(pipeline, steps):
    c = P.load(pipeline)
    tr = _traces(c)
    if not tr:
        return None, {"status": "error", "error": "this pipeline has no traces yet"}
    try:
        return E.run(tr, steps, E.index_dir_of(pipeline)), None
    except E.PlanError as e:
        return None, {"status": "error", "error": str(e)}


def register(mcp):
    @mcp.tool()
    def query_summary(pipeline: str) -> dict:
        """What the provenance holds, read with TraceViewer: the entity types (role, description), entities by type, activities by name, the time span.
        Use first, to turn the user's words into real types and names. Returns {types, entities_by_type, activities_by_name, queries}."""
        r, err = _run(pipeline, [{"op": "entities", "all": True}, {"op": "group", "from": 0, "by": "type"}, {"op": "activities"}, {"op": "group", "from": 2, "by": "name"}])
        if err:
            return err
        _log(pipeline, "summary", r["steps"][1]["headline"])
        return {"types": r["types"], "entities_by_type": r["outs"][1]["counts"], "activities_by_name": r["outs"][3]["counts"],
                "activities": r["steps"][2]["headline"], "queries": [q["query"] for q in r["queries"]]}

    @mcp.tool()
    def query_entities(pipeline: str, type: str = "", match: str = "", limit: int = 100) -> dict:
        """Find entities with a TraceViewer query, by exact type and/or a substring of their uri. Use to resolve what the user named into entity ids.
        Returns {count, entities: [{id, type, store, uri}], query}."""
        r, err = _run(pipeline, [{"op": "entities", **({"type": type} if type else {}), **({"uri_contains": match} if match else {}), **({} if type or match else {"all": True})}])
        if err:
            return err
        ids = r["outs"][0]["ids"]
        _log(pipeline, f"entities type={type} match={match}", len(ids))
        rows = [x for x in (r["steps"][0]["table"] or "").splitlines()[2:]][:limit]
        return {"count": len(ids), "entities": rows, "ids": ids[:limit], "query": r["queries"][-1]["query"]}

    @mcp.tool()
    def query_lineage(pipeline: str, entity: str, direction: str = "back") -> dict:
        """Walk the lineage with TraceViewer queries (one per hop) from the entities matching ``entity``: an id, a type, or a part of the uri. direction=back: what it came
        from; forward: what it fed. For exploring; the user's answer comes from evidence_answer. Returns {result, table, hops}."""
        r, err = _run(pipeline, [{"op": "entities", "match": entity}, {"op": "lineage", "from": 0, "direction": direction}])
        if err:
            return err
        _log(pipeline, f"lineage {direction} {entity}", r["steps"][1]["headline"][:200])
        return {"result": r["steps"][1]["headline"], "table": r["steps"][1]["table"][:6000], "hops": len(r["outs"][1]["hops"])}

    @mcp.tool()
    def evidence_answer(pipeline: str, question: str, steps: list) -> dict:
        """Answer the user's provenance question - lineage (where did X come from), downstream impact (what did X affect), what happened when - with
        evidence. Call it for every such question, after resolving the user's words into real types, ids and time frames (query_summary, query_entities).
        Nothing here is written by you: each step is a TraceViewer query on the traces, the result is computed from the records, and the answer is kept
        with its trail (queries, files read, cited records) for the Query view.
        steps: [{op, ...}], "from" = the 0-based index of an earlier step:
          entities {type?, uri_contains?, id?, ids?, all?, produced_since?, produced_until?} | activities {name?, pid?, since?, until?} |
          lineage {from, direction: back|forward, max_hops?} | group {from, by: type|role|store|name|pid} | ends {from} (roots of back, leaves of forward) |
          inputs {activity, since?, until?} (the parameters each run used: arguments, object attributes, command-line flags) | outputs {activity} (what it
          returned) | attributes {from} (the attributes recorded for a step's entities) | stats {name, type?} or {name, from} (n, mean, median, min, max of
          a recorded value over EVERY entity that records it - a mean or a range over a run is this step, never a sample or a script).
          Times: ISO, epoch seconds or microseconds, or start+30m / end-2h.
          The four aspects (one capability each): traceability = the steps above; accountability = context {} (who ran it, group, job, hosts,
          processes, tools; user/group are hashes, resolved unless the trace is anonymized) | timeline {name?}; decision making = stats |
          status {names?} (what failed or passed); performance = perf_time {by: activity|function|io|process|host|layer} | perf_io {by: file|call|host}
          | perf_parallelism {} | perf_system {metric: cpu|disk|network}.
        Example, "what is the bottleneck": [{"op":"perf_time","by":"activity"}]; "who ran this": [{"op":"context"}]; "did any step fail": [{"op":"status"}].
        Example, "what are the filter parameters": [{"op":"inputs","activity":"filter_msa"}].
        Example, "what is the mean pLDDT of the predicted structures": [{"op":"stats","name":"mean_plddt","type":"best_structure"}].
        Example, "where did the top-5 summary come from": [{"op":"entities","type":"topk_summary","uri_contains":"top5"},{"op":"lineage","from":0,"direction":"back"},{"op":"ends","from":1}].
        Returns {id, steps: [{step, op, resolved, result, table}], result_hash, stats}. Then write the user's answer from these results ONLY - as your
        chat reply: the direct answer first, then spread, coverage, method, caveats - and attach it with evidence_synthesis(pipeline, id, text), citing
        steps as [S1]..[Sn]; the Query view shows it as the answer."""
        try:
            rec = E.ask(pipeline, question, steps, chose="agent", chat=os.environ.get("MOIRAI_AGENTS_WEBUI_SESSION", ""))
        except E.PlanError as e:
            return {"status": "error", "error": str(e), "fix": "resolve the names with query_summary / query_entities, then fix the plan"}
        _log(pipeline, f"answer: {question}", rec["result"]["headline"][:200])
        return {"status": "ok", "id": rec["id"], "steps": [{"step": s["step"], "op": s["op"], "resolved": s["resolved"], "result": s["headline"], "table": s["table"][:4000]}
                                                           for s in rec["steps"]], "result_hash": rec["result_hash"], "stats": rec["stats"]}

    @mcp.tool()
    def evidence_synthesis(pipeline: str, id: str, text: str) -> dict:
        """Attach your write-up for the user to a computed answer (evidence_answer's id): the same text as your chat reply, shown as the answer in the
        Query view. Short Markdown, the direct answer in its first sentence; every claim cites the step it rests on as [S1]..[Sn]; use only the numbers
        and names in the computed results, the question or the run's name. It is checked: a citation to no step, or a number not in the results, is flagged to the
        user next to it - fix and call again. Returns {id, synthesis_check: {ok, problems, citations}}."""
        try:
            return E.synthesize(pipeline, id, text)
        except E.PlanError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def evidence_rerun(pipeline: str, id: str) -> dict:
        """Repeat a kept answer's plan on the traces as they are now (a live run's traces grow). Returns {same, result_hash, was, files_changed, headline}."""
        try:
            return E.rerun(pipeline, id)
        except E.PlanError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def query_dataframe(pipeline: str, code: str, timeout: int = 300) -> dict:
        """Ad-hoc query with the dftracer-utils DataFrame API, in the moirai Python. Use when the other query tools cannot answer. code: Python that prints its answer; already defined are
        DataFrame, TraceViewer, col, tv (the traces), graph (provenance records), acts (activities). Relation lists read as columns args.relations.used.0, ... Returns {status, stdout, stderr}."""
        c = P.load(pipeline)
        m = M.locate()
        if not m.get("dftracer_utils"):
            return {"status": "error", "error": "dftracer-utils is not in the moirai Python", "fix": m.get("fix", "")}
        t = os.path.abspath(os.path.expanduser(_traces(c)))
        trace_dir = t if os.path.isdir(t) else str(Path(t.split("*")[0]).parent)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "q.py"
            f.write_text(PREAMBLE + "\n" + code)
            env = {**os.environ, "MOIRAI_Q_TRACES": trace_dir, "MOIRAI_Q_INDEX": str(P.pdir(pipeline) / "index")}
            r = subprocess.run([m["python"], str(f)], capture_output=True, text=True, timeout=timeout, env=env, cwd=d)
        _log(pipeline, code, r.stdout[:200] or r.stderr[-200:])
        return {"status": "ok" if r.returncode == 0 else "error", "stdout": r.stdout[-20000:], "stderr": r.stderr[-3000:]}

    @mcp.tool()
    def query_validate(pipeline: str) -> dict:
        """Validate the analysis pipeline's query stage: the traces are readable, and the questions the user asked were run and logged. Use before its last gate.
        A run pipeline has no query stage: do not call this for one. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        if STAGE not in P.KINDS.get(c["kind"], []):
            return {"status": "error", "error": f"a {c['kind']} pipeline has no query stage: its questions are answered in the Query view, with no gate"}
        q = P.stage(c, STAGE)["outputs"].get("queries", [])
        v = P.Verdict(STAGE)
        v.check("asked", bool(q), "no query has run", "run the user's questions")
        r, err = _run(pipeline, [{"op": "entities", "all": True}])
        v.check("graph", bool(r) and r["stats"]["rows_read"] > 0, (err or {}).get("error", "no provenance records"), "check the traces")
        P.record(pipeline, STAGE, outputs={"summary": f"{len(q)} queries run"})
        return P.finish_validation(pipeline, v)
