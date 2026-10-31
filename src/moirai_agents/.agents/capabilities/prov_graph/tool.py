"""Run pipeline stage 8: the graph health gate. The run is done only when every artifact has a producer and reaches real inputs.

Moved from dftracer-agents' provenance mode, reading moirai's records.
"""
import hashlib
import json
from collections import Counter
from pathlib import Path

from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGE = "graph"
ROOT_ROLES = {"input", "parameter", "reference"}


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


def _digest(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def register(mcp):
    @mcp.tool()
    def graph_check(pipeline: str) -> dict:
        """Build the provenance graph of the baseline run and measure its health against the spec, and write <pipeline>/graph.json. Use after the baseline.
        Returns {activities, entities, entities_by_type, dangling, artifact_types_missing, artifacts_without_producer, types_undescribed, roots_not_inputs, lineage_example}."""
        c = P.load(pipeline)
        traces = P.stage(c, "baseline")["outputs"].get("traces")
        if not traces:
            return {"status": "error", "error": "the baseline stage has no traces"}
        spec = P.stage(c, "annotate")["outputs"].get("spec") or {}
        g = T.read(traces)
        h = _health(g, spec)
        (P.work(c) / "graph.json").write_text(json.dumps({**g.view(10**9), "health": h}, default=str))
        P.record(pipeline, STAGE, status="running", outputs={"health": h, "summary": f"{h['artifacts']} artifact(s), {h['activities']} activities, {h['dangling']} dangling"}, note="graph built")
        return h

    @mcp.tool()
    def graph_compare(pipeline: str, plain_dir: str = "", traced_dir: str = "") -> dict:
        """Check the run did not change the science: compare the files the plain run wrote with the same files from the traced run. Use after graph_check. plain_dir: default where smoke ran.
        traced_dir: default the annotated tree. Returns {identical, different, missing}; a difference in a file with timestamps may be harmless, so it is for the user to judge."""
        c = P.load(pipeline)
        smoke = P.stage(c, "smoke")["outputs"]
        a, b = Path(plain_dir or smoke.get("cwd") or P.source_of(c)), Path(traced_dir or P.stage(c, "integrate")["outputs"].get("annotated", ""))
        out = {"identical": [], "different": [], "missing": []}
        for rel in smoke.get("outputs_created", []):
            if not (b / rel).is_file():
                out["missing"].append(rel)
            elif (a / rel).is_file():
                out["identical" if _digest(a / rel) == _digest(b / rel) else "different"].append(rel)
        P.record(pipeline, STAGE, outputs={"compare": {k: len(v) for k, v in out.items()}, "compare_differs": out["different"][:20]}, note="outputs compared")
        return out

    @mcp.tool()
    def graph_validate(pipeline: str) -> dict:
        """The health gate: provenance was recorded, no dangling id, every artifact type present with a producer, every root a real input, every type described. Use before the gate.
        Whether the science changed is reported as inconclusive when outputs differ. Returns the verdict {status, failed, unknown, passed, total}."""
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
            if o.get("compare_differs"):
                v.inconclusive("science_unchanged", f"outputs differ from the plain run: {o['compare_differs'][:5]}")
            elif "compare" not in o:
                v.inconclusive("science_unchanged", "graph_compare has not run")
        return P.finish_validation(pipeline, v)
