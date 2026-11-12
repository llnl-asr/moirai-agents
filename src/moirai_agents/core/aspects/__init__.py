"""The four aspects of provenance, each its own module (and its own capability: prov_traceability, prov_accountability, prov_decision,
prov_performance), so each is maintained and extended on its own:

  traceability     where did this come from: workflow and app instance, process, function, code region, parameters, attributes
  accountability   who did what, when, with what: the person and their group, app, process, function, time, tools and versions
  decision         why trust the result: quality, quantity, metrics, what failed or passed
  performance      how to be more efficient: bottlenecks, time per function and layer, I/O, parallelism, app- vs system-centric

Every aspect module declares:
  ASPECT, LABEL, SCOPE        its name, how the UI names it, and what it covers (one paragraph)
  CORE_OPS                    query operations it owns that the evidence engine implements itself (core.evidence)
  OPS                         {op: fn(s, k, v, cat, outs, ctx)} operations it implements; each returns an "aspect" output (see result())
  OPWORD                      {op: how the plan reads}
  CAPTURE                     what the annotation must record for this aspect (the aspect's skill holds the how)

Every read goes through the evidence engine's Viewer, which reads through core.provtrace (the prov_read capability): TraceViewer only.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import accountability, decision, performance, traceability

MODULES = (performance, accountability, decision, traceability)      # most specific first: a plan's category is its most specific aspect
ASPECTS = {m.ASPECT: m for m in MODULES}


def ops() -> Dict[str, Callable]:
    out: Dict[str, Callable] = {}
    for m in MODULES:
        out.update(m.OPS)
    return out


def all_ops() -> List[str]:
    return [o for m in reversed(MODULES) for o in (*m.CORE_OPS, *m.OPS)]


def aspect_of(op: str) -> Optional[str]:
    for m in MODULES:
        if op in m.OPS or op in m.CORE_OPS:
            return m.ASPECT
    return None


def label_of_plan(steps: List[Dict[str, Any]]) -> str:
    """The aspect a plan answers: its most specific step's aspect."""
    found = {aspect_of(str(s.get("op", ""))) for s in steps or []}
    for m in MODULES:
        if m.ASPECT in found:
            return m.LABEL
    return "Other"


def opword() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in MODULES:
        out.update(m.OPWORD)
    return out


def result(aspect: str, headline: str, columns: List[str], rows: List[List[Any]], resolved: Dict[str, Any], cites: List[Dict[str, Any]],
           gaps: Optional[List[Dict[str, Any]]] = None, ids: Optional[List[str]] = None, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The output of an aspect operation: the engine shows headline and the table as given; gaps join the answer's gaps."""
    return {"kind": "aspect", "aspect": aspect, "headline": headline, "columns": columns, "rows": rows, "resolved": resolved, "cites": cites,
            "ids": ids or [], "gaps": gaps or [], "values": values or {}}


# ------------------------------------------------------------------ what each aspect capability runs: its summary, its coverage check, its selftest
SUMMARY = {
    "traceability": [[{"op": "entities", "all": True}, {"op": "group", "from": 0, "by": "type"}], [{"op": "activities"}, {"op": "group", "from": 0, "by": "name"}]],
    "accountability": [[{"op": "context"}], [{"op": "timeline"}], [{"op": "configuration"}]],
    "decision": [[{"op": "status"}], [{"op": "entities", "all": True}, {"op": "group", "from": 0, "by": "type"}]],
    "performance": [[{"op": "perf_time", "by": "activity"}], [{"op": "perf_time", "by": "layer"}], [{"op": "perf_io", "by": "file"}],
                    [{"op": "perf_parallelism"}], [{"op": "perf_system", "metric": "cpu"}]],
}
# (check, plan index in SUMMARY, test on that plan's last step, required, fix): required=False is advice (a warning), not a failure
CHECKS = {
    "traceability": [("entities_declared", 0, lambda st, o: bool(o.get("counts")), True, "declare the scientific artifacts as entity types and record them (prov_annotate spec)"),
                     ("activities_recorded", 1, lambda st, o: bool(o.get("counts")), True, "annotate the functions that read or write the artifacts as activities"),
                     ("parameters_captured", 0, lambda st, o: any(str(k).endswith(".input") for k in (o.get("counts") or {})), False,
                      "capture each activity's parameters with capture.inputs (prov_annotate)"),
                     ("attributes_captured", 0, lambda st, o: any(str(k).endswith(".attribute") for k in (o.get("counts") or {})), False,
                      "record the facts of each artifact as attributes (prov_annotate entity attributes)")],
    "accountability": [("who_ran_it", 0, lambda st, o: bool((o.get("values") or {}).get("context", {}).get("users")), True,
                        "record the run_context entity (user, group, job, versions): prov_annotate's helper does it at start; re-annotate and re-run"),
                       ("processes_recorded", 0, lambda st, o: bool(o.get("rows")), True, "keep dftracer metadata on (MOIRAI_INC_METADATA=1) so each process's start record is written"),
                       ("hosts_named", 0, lambda st, o: bool((o.get("values") or {}).get("hosts")), True, "keep dftracer metadata on (host names: HH records)"),
                       ("activities_timed", 1, lambda st, o: bool(o.get("rows")), True, "annotate the functions as activities: each run carries its start and duration"),
                       ("configuration_recorded", 2, lambda st, o: bool(o.get("rows")), False,
                        "run with a moirai that records process metadata at initialize (user/group hashes, job, env.MOIRAI_*/DFTRACER_*/OMP_*, launcher)")],
    "decision": [("outcomes_recorded", 0, lambda st, o: bool(o.get("rows")), True,
                  "capture each step's outcome as an output (returncode=, status=, ok=, error=) with capture.outputs"),
                 ("quality_metrics_recorded", 1, lambda st, o: any(str(k).endswith(".attribute") for k in (o.get("counts") or {})), True,
                  "record quality and quantity metrics as attributes of the entities they describe (score=, n_items=)")],
    "performance": [("activities_timed", 0, lambda st, o: bool(o.get("rows")), True, "annotate the functions as activities: each run carries its duration"),
                    ("io_traced", 2, lambda st, o: bool(o.get("rows")) or "more calls on no file" in o.get("headline", ""), True,
                     "run with dftracer I/O tracing on (MOIRAI_TRACE_IO=1, set by moirai_integrate)"),
                    ("system_counters", 4, lambda st, o: bool(o.get("rows")), False, "keep dftracer's system counters on (psutil sys/io/net)"),
                    ("app_functions_annotated", 1, lambda st, o: "0.000 s in annotated functions" not in o.get("headline", ""), False,
                     "performance-annotate the app's hot code with dftracer (prov_performance: perf_annotate)")],
}


def _traces(pipeline: str = "", traces: str = ""):
    from .. import answers as A
    from .. import evidence as E
    from .. import pipeline as P
    from .. import provtrace as T
    if traces:
        files = T.expand(traces)
        return traces, (T.index_dir(files) if files else "")
    c = P.load(pipeline)
    tr = A.traces_of(c)
    if not tr:
        raise P.PipelineError(f"{pipeline} has no traces yet")
    return tr, E.index_dir_of(pipeline)


def summary(aspect: str, pipeline: str = "", traces: str = "") -> Dict[str, Any]:
    """The aspect's standard questions on a pipeline's (or a trace dir's) traces: each one's headline, table and gaps, through TraceViewer."""
    from .. import evidence as E
    tr, idx = _traces(pipeline, traces)
    out = []
    for plan in SUMMARY[aspect]:
        r = E.run(tr, plan, idx)
        st = r["steps"][-1]
        out.append({"plan": plan, "headline": st["headline"], "table": st["table"], "gaps": r["gaps"], "_o": r["outs"][-1]})
    return {"aspect": aspect, "label": ASPECTS[aspect].LABEL, "scope": ASPECTS[aspect].SCOPE, "traces": tr,
            "answers": [{k: v for k, v in a.items() if k != "_o"} for a in out], "_outs": [a["_o"] for a in out]}


def check(aspect: str, pipeline: str = "", traces: str = "") -> Dict[str, Any]:
    """Whether the annotation captured what this aspect needs: one check per need, each with the fix. status: pass (every required check
    holds), warn (only advice is missing), fail."""
    s = summary(aspect, pipeline, traces)
    checks = []
    for name, i, test, required, fix in CHECKS[aspect]:
        try:
            ok = bool(test(s["answers"][i], s["_outs"][i]))
        except Exception:  # noqa: BLE001
            ok = False
        checks.append({"name": name, "ok": ok, "required": required, "detail": s["answers"][i]["headline"][:240], "fix": "" if ok else fix})
    bad = [c for c in checks if not c["ok"] and c["required"]]
    warn = [c for c in checks if not c["ok"] and not c["required"]]
    return {"aspect": aspect, "status": "fail" if bad else "warn" if warn else "pass", "failed": [c["name"] for c in bad],
            "advice": [c["name"] for c in warn], "checks": checks, "capture": ASPECTS[aspect].CAPTURE}


SELFTEST = {   # on core.aspects._selftest's trace: what each aspect's summary must say
    "traceability": ["By name: clean 1, plot 1", "raw 1", "table.attribute 1"],
    "accountability": ["Run by alice (group lab), job f1234", "node01", "first clean", "env.OMP_NUM_THREADS", "/home/alice/run"],
    "decision": ["1 recorded outcome(s) failed and 1 passed", "plot.returncode"],
    "performance": ["clean (3.000 s, 75.0%)", "4.00 KiB read and 1.00 KiB written", "average concurrency of 1.33", "user 50.0%"],
}


def selftest(aspect: str) -> Dict[str, Any]:
    """The aspect's summary and check on a synthetic trace with every aspect in it: the expected statements must all appear."""
    import tempfile
    from . import _selftest as ST
    with tempfile.TemporaryDirectory(prefix=f"aspect_{aspect}_") as d:
        ST.write(d)
        s = summary(aspect, traces=d)
        c = check(aspect, traces=d)
    text = " ".join(a["headline"] for a in s["answers"]) + " " + " ".join(a["table"] for a in s["answers"])
    missing = [x for x in SELFTEST[aspect] if x not in text]
    ok = not missing and c["status"] in ("pass", "warn")
    return {"status": "ok" if ok else "error", "missing": missing, "check": c["status"], "failed": c["failed"],
            "line": f"{aspect} selftest {'PASS' if ok else 'FAIL'}" + (f": missing {missing}" if missing else "") + (f"; check {c['status']} {c['failed']}" if c["failed"] else "")}
