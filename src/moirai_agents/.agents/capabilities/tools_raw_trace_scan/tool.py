"""Statistics of one recorded attribute (e.g. mean_plddt of best_structure) over every entity of a pipeline's traces, read through
dftracer-utils' TraceViewer (the evidence engine's stats query), never by parsing the trace files. When the shared index beside the traces
is locked by another process, a private TraceViewer index under the project's state dir is used instead, so a LOCK never blocks the answer."""
import hashlib
import json
import tempfile
from pathlib import Path

from moirai_agents.core import evidence as E
from moirai_agents.core import provtrace as T
from moirai_agents.env import state_dir


def _private_index(files):
    """A TraceViewer index of these files of our own: used only when the shared one is locked."""
    key = hashlib.sha1("\n".join(sorted(files)).encode()).hexdigest()[:16]
    d = state_dir("cache", "tv-index", key)
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _stats(trace_dir, attr, etype, idx=""):
    files = T.expand(trace_dir)
    if not files:
        return {"files": 0, "by_type": {}, "index": ""}
    step = {"op": "stats", "name": attr, **({"type": etype} if etype else {})}
    idx = idx or T.index_dir(files)
    try:
        r = E.run(trace_dir, [step], idx)
    except Exception as e:  # noqa: BLE001  the shared index is held by another process (RocksDB LOCK): our own index, never the files
        if "lock" not in str(e).lower():
            raise
        idx = _private_index(files)
        r = E.run(trace_dir, [step], idx)
    out = r["steps"][-1]
    groups = (r.get("outs") or [{}])[-1].get("groups") or []
    by = {g["of"]: {"n": g["n"], "distinct_values": g["distinct"], "mean": g["mean"], "mean_over_distinct": g.get("mean_distinct"),
                    "median": g["median"], "min": g["min"], "max": g["max"], "min_owner": g.get("min_owner", []), "max_owner": g.get("max_owner", [])}
          for g in groups if g.get("n")}
    return {"files": len(files), "by_type": by, "index": idx, "headline": out.get("headline", ""), "result_hash": r.get("result_hash", "")}


def register(mcp):
    @mcp.tool()
    def raw_attribute_scan(trace_dir: str, attribute: str = "mean_plddt", entity_type: str = "") -> dict:
        """Statistics of one recorded attribute over every entity of a trace dir (a pipeline's traces/.../raw), read through TraceViewer:
        the `<entity>.attribute` records `<attribute>=<number>`. entity_type limits it to one entity (e.g. best_structure). Each value counts
        once per entity it describes (identical values share one attribute record), so mean is over entities and mean_over_distinct over the
        distinct values: say which one you quote. When the shared index is locked by another process, a private index is built instead
        (slower the first time). Prefer evidence_answer with a stats step: it is cited in the Query view; this tool's result is not.
        Returns {status, files, by_type: {type: {n, distinct_values, mean, mean_over_distinct, median, min, max, min_owner, max_owner}}, index, headline}."""
        if not Path(trace_dir).expanduser().is_dir():
            return {"status": "error", "error": f"{trace_dir} is not a directory", "fix": "pass the raw trace dir of the pipeline"}
        try:
            r = _stats(trace_dir, attribute, entity_type)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)[:500], "fix": "check the attribute name against the entity description (prov_query)"}
        if not r["files"]:
            return {"status": "error", "error": "no trace files in " + trace_dir}
        return {"status": "ok", **r}

    @mcp.tool()
    def raw_scan_selftest() -> dict:
        """Self-check on a synthetic trace read through TraceViewer: two best_structure mean_plddt attribute records (70, 90) must give
        n 2, mean 80.0, min 70.0, max 90.0. Returns {status, got}."""
        import gzip
        with tempfile.TemporaryDirectory() as d:
            recs = [{"name": "ET", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"type": "best_structure.attribute", "description": "a value"}}]
            recs += [{"name": "EH", "cat": "dftracer", "ph": 4, "pid": 1,
                      "args": {"id": "%016x" % i, "type": "best_structure.attribute", "uri": f"mean_plddt={v}", "store": 0}} for i, v in enumerate((70.0, 90.0))]
            with gzip.open(Path(d) / "a.pfw.gz", "wt") as fh:         # writing a synthetic trace; it is read only through TraceViewer
                fh.write("".join(json.dumps(r) + "\n" for r in recs))
            g = _stats(d, "mean_plddt", "best_structure")["by_type"].get("best_structure", {})
        ok = g.get("n") == 2 and g.get("mean") == 80.0 and g.get("min") == 70.0 and g.get("max") == 90.0
        return {"status": "ok" if ok else "error", "got": {k: g.get(k) for k in ("n", "mean", "min", "max")}}
