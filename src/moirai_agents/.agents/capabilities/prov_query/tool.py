"""Explore provenance: built-in graph questions, and the dftracer-utils DataFrame API for anything ad hoc. Shared by both pipelines' last stage."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from moirai_agents.core import moirai_env as M
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

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
    if c["kind"] == "analysis":
        return c["traces"]
    o = P.stage(c, "baseline")["outputs"]
    return o.get("traces") or P.stage(c, "moirai_smoke")["outputs"].get("traces", "")


def _log(pipeline, what, result):
    def fn(c):
        s = c["stages"][STAGE]
        s["status"] = "running"
        s["outputs"].setdefault("queries", []).append({"q": what[:300], "result": str(result)[:300]})
        s["rev"] += 1
        return {"kind": "query", "q": what[:200]}
    P.update(pipeline, fn)


def register(mcp):
    @mcp.tool()
    def query_summary(pipeline: str) -> dict:
        """What the provenance holds: counts, entities by type and store, activity kinds with total time, edge and relation kinds, entity type roles. Use first when exploring.
        Returns {activities, entities, edges, by_type, by_store, activity_kinds, ...}."""
        c = P.load(pipeline)
        r = T.read(_traces(c)).summary()
        _log(pipeline, "summary", {k: r[k] for k in ("activities", "entities", "edges")})
        return r

    @mcp.tool()
    def query_entities(pipeline: str, type: str = "", match: str = "", limit: int = 100) -> dict:
        """List entities, by exact type and/or a substring of their uri. Use to find the id of what you want to trace. Returns {count, entities: [{id, type, store, uri}]}."""
        g = T.read(_traces(P.load(pipeline)))
        rows = [e for e in g.entities.values() if (not type or e["type"] == type) and (not match or match in e.get("uri", ""))]
        _log(pipeline, f"entities type={type} match={match}", len(rows))
        return {"count": len(rows), "entities": rows[:limit]}

    @mcp.tool()
    def query_lineage(pipeline: str, entity: str, direction: str = "back") -> dict:
        """Walk the graph from an entity (its id, a uri substring, or a type). direction=back: everything it came from. direction=forward: everything it fed. Use for 'where did this
        come from' and 'what used this'. Returns {start, hops, steps: [{hop, activity, from, to}], reached, ends}."""
        r = T.read(_traces(P.load(pipeline))).lineage(entity, direction)
        _log(pipeline, f"lineage {direction} {entity}", r.get("reached", r.get("error")))
        return r

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
        """Validate the query stage: the traces are readable, and the questions the user asked were run and logged. Use before the last gate. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        q = P.stage(c, STAGE)["outputs"].get("queries", [])
        v = P.Verdict(STAGE)
        v.check("asked", bool(q), "no query has run", "run the user's questions")
        v.check("graph", T.read(_traces(c)).counts()["records"] > 0, "no records", "check the traces")
        P.record(pipeline, STAGE, outputs={"summary": f"{len(q)} queries run"})
        return P.finish_validation(pipeline, v)
