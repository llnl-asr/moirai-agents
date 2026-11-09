"""Run pipeline stage 8: the graph. Build the provenance graph of the baseline run, run the graph inference algorithms, and gate
on its health: every artifact has a producer and reaches real inputs.

Moved from dftracer-agents' provenance mode, reading moirai's records. The graph is read through core.answers.graph, the
incremental per-pipeline reader the query tools share, so the graph stage and every later question read each record once.
Comparing the plain and the traced run's outputs is not part of this stage (v2 did it; see versions/v2).
"""
import json
from collections import Counter

from moirai_agents.core import answers as A
from moirai_agents.core import pipeline as P

STAGE = "graph"
ROOT_ROLES = {"input", "parameter", "reference"}

# Graph inference algorithms: (name, fn(graph, spec) -> dict). Each runs in graph_check, after the graph is built, and its
# result is recorded under outputs.inferences[name]. None yet: add one here (with a check in checks.json) when it exists.
INFERENCES = []


def _health(g, spec):
    declared = {t["type"]: t for t in spec.get("entity_types", [])}
    artifacts = spec.get("scientific_artifacts", [])
    makers, users = {}, {}
    for e in g.edges:
        (makers if e["kind"] in ("generated", "updated") else users if e["kind"] == "used" else {}).setdefault(e["entity"], []).append(e["activity"])
    inputs = {}
    for e in g.edges:
        if e["kind"] == "used":
            inputs.setdefault(e["activity"], []).append(e["entity"])
    by_type = Counter(e["type"] for e in g.entities.values())
    lineage, unproduced, bad_roots = {}, [], {}
    for eid, e in g.entities.items():
        if e["type"] not in artifacts:
            continue
        if eid not in makers:
            unproduced.append(eid)
        seen, stack, roots = set(), [eid], set()
        while stack:
            cur = stack.pop()
            ms = makers.get(cur, [])
            if not ms and cur != eid:
                roots.add(cur)
            for a in ms:
                for u in inputs.get(a, []):
                    if u not in seen:
                        seen.add(u)
                        stack.append(u)
        types = {g.entities.get(r, {}).get("type", "?") for r in roots}
        lineage[eid] = {"type": e["type"], "uri": e.get("uri", ""), "ancestors": len(seen), "root_types": sorted(types)}
        wrong = sorted(t for t in types if declared.get(t, {}).get("role") not in ROOT_ROLES)
        if wrong:
            bad_roots[eid] = wrong
    return {"records": g.counts()["records"], "activities": len(g.activities), "entities": len(g.entities), "entities_by_type": dict(by_type), "dangling": len(g.dangling()),
            "artifact_types_missing": sorted(a for a in artifacts if not by_type.get(a)), "artifacts_without_producer": len(unproduced),
            "types_undescribed": sorted(t for t in by_type if not (g.types.get(t) or {}).get("description")),
            "roots_not_inputs": bad_roots, "lineage_example": next(iter(lineage.values()), None), "artifacts": len(lineage)}


def _infer(g, spec):
    out = {}
    for name, fn in INFERENCES:
        try:
            out[name] = fn(g, spec)
        except Exception as e:  # noqa: BLE001  one failing algorithm must not hide the graph's health
            out[name] = {"status": "error", "error": str(e)[:300]}
    return out


def register(mcp):
    @mcp.tool()
    def graph_check(pipeline: str) -> dict:
        """Build the provenance graph of the baseline run, run the graph inference algorithms, measure its health against the spec, and write <pipeline>/graph.json.
        Use after the baseline. Returns {activities, entities, entities_by_type, dangling, artifact_types_missing, artifacts_without_producer, types_undescribed,
        roots_not_inputs, lineage_example, inferences}."""
        c = P.load(pipeline)
        if not P.stage(c, "baseline")["outputs"].get("traces"):
            return {"status": "error", "error": "the baseline stage has no traces"}
        spec = P.stage(c, "annotate")["outputs"].get("spec") or {}
        g, _ = A.graph(c)
        h = _health(g, spec)
        h["inferences"] = _infer(g, spec)
        (P.work(c) / "graph.json").write_text(json.dumps({**g.view(10**9), "health": h}, default=str))
        P.record(pipeline, STAGE, status="running", outputs={"health": h, "summary": f"{h['artifacts']} artifact(s), {h['activities']} activities, {h['dangling']} dangling"}, note="graph built")
        return h

    @mcp.tool()
    def graph_validate(pipeline: str) -> dict:
        """The health gate: provenance was recorded, no dangling id, every artifact type present with a producer, every root a real input, every type described,
        and no graph inference algorithm failed. Use before the gate. Returns the verdict {status, failed, unknown, passed, total}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        h = o.get("health")
        if v.check("built", bool(h), "graph_check has not run", "run graph_check"):
            v.check("recorded", h["activities"] > 0, "no activity in the trace", "did the annotated code run?")
            v.check("no_dangling", h["dangling"] == 0, f"{h['dangling']} ids with no EH record", "a process's trace is missing, or an entity is related but never declared")
            v.check("artifacts_present", not h["artifact_types_missing"], f"missing: {h['artifact_types_missing']}", "annotation gap at the writer, or the run did not reach it")
            v.check("artifacts_produced", h["artifacts_without_producer"] == 0, f"{h['artifacts_without_producer']} without a producer", "add generated at the writer: every artifact needs a producer activity")
            v.check("roots_are_inputs", not h["roots_not_inputs"], str(h["roots_not_inputs"])[:300], "a producer upstream is not annotated: walk back and add it")
            v.check("types_described", not h["types_undescribed"], f"undescribed: {h['types_undescribed']}", "declare the type with its description in the spec, and annotate again")
            bad = sorted(k for k, r in (h.get("inferences") or {}).items() if isinstance(r, dict) and r.get("status") == "error")
            if (h.get("inferences") or {}):
                v.check("inferences_ran", not bad, f"failed: {bad}", "fix the algorithm in prov_graph's INFERENCES, then graph_check again")
        return P.finish_validation(pipeline, v)
