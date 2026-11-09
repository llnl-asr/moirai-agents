"""The aggregated view: the entity-relationship graph of provenance - entity types and activity kinds as nodes, how they
relate (used, generated, updated, invalidated, entity relations) as edges - built two ways and compared:

- expected(spec): what the annotation says will be recorded (the spec the annotate stage applied),
- runtime(graph): what a run actually recorded, with counts (every entity, activity and edge folded onto its type),
- diff(expected, runtime): every node and edge marked matched, missing (expected, never recorded), conditional (expected
  only when a condition holds, not recorded) or unexpected (recorded, not in the spec).

The Annotate view shows the diff after the baseline; the Live Run and Query views show the runtime graph as it grows.
Node ids: ``t:<entity type>``, ``a:<activity name>``. Edge ids: ``<kind>|<source>|<target>[|<relation>]``.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

ACT_EDGES = ("used", "generated", "updated", "invalidated")


def _edge(kind: str, src: str, dst: str, rel: str = "") -> Dict[str, Any]:
    return {"id": "|".join(x for x in (kind, src, dst, rel) if x), "source": src, "target": dst, "kind": kind, "label": rel or ""}


def expected(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The aggregated graph the spec promises."""
    spec = spec or {}
    arts = set(spec.get("scientific_artifacts") or [])
    roles = {e["type"]: e.get("role", "") for e in spec.get("entity_types", []) if isinstance(e, dict) and e.get("type")}
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}

    def tnode(t: str) -> str:
        nodes.setdefault(f"t:{t}", {"id": f"t:{t}", "kind": "type", "label": t, "role": roles.get(t, ""), "artifact": t in arts})
        return f"t:{t}"

    for t in roles:
        tnode(t)
    for a in spec.get("activities", []):
        name = a.get("name")
        if not name:
            continue
        aid = f"a:{name}"
        nodes.setdefault(aid, {"id": aid, "kind": "activity", "label": name, "file": a.get("file", "")})
        for k in ACT_EDGES:
            for ent in a.get(k, []) or []:
                t = (ent or {}).get("type")
                if not t:
                    continue
                e = _edge(k, tnode(t), aid) if k == "used" else _edge(k, aid, tnode(t))
                cur = edges.setdefault(e["id"], {**e, "conditional": True})
                cur["conditional"] = cur["conditional"] and bool(ent.get("when"))      # unconditional once any declaration is unconditional
        for r in a.get("relations", []) or []:
            s, o = (r.get("subject") or {}).get("type"), (r.get("object") or {}).get("type")
            if s and o:
                e = _edge("relation", tnode(s), tnode(o), str(r.get("relation", "")))
                cur = edges.setdefault(e["id"], {**e, "conditional": True})
                cur["conditional"] = cur["conditional"] and bool(r.get("when"))
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def runtime(g: Any) -> Dict[str, Any]:
    """The aggregated graph of what was recorded (a provtrace.Graph), with counts."""
    roles = {t: (d or {}).get("role", "") for t, d in g.types.items()}
    etype = {i: e.get("type", "") or "?" for i, e in g.entities.items()}
    aname = {a["id"]: a["name"] for a in g.activities}
    tc = Counter(etype.values())
    ac = Counter(aname.values())
    nodes = {f"t:{t}": {"id": f"t:{t}", "kind": "type", "label": t, "role": roles.get(t, ""), "count": n} for t, n in tc.items()}
    nodes.update({f"a:{n}": {"id": f"a:{n}", "kind": "activity", "label": n, "count": k} for n, k in ac.items()})
    ec: Counter = Counter()
    for e in g.edges:
        t, a = etype.get(e["entity"]), aname.get(e["activity"])
        if not t or not a:
            continue                       # dangling: counted by the graph's own health check, not drawn here
        ec[_edge(e["kind"], f"t:{t}", f"a:{a}")["id"] if e["kind"] == "used" else _edge(e["kind"], f"a:{a}", f"t:{t}")["id"]] += 1
    for r in g.relations:
        s, o = etype.get(r["subject"]), etype.get(r.get("object", ""))
        if s and o:
            ec[_edge("relation", f"t:{s}", f"t:{o}", str(r["relation"]))["id"]] += 1
    edges = []
    for eid, n in ec.items():
        parts = eid.split("|")
        e = _edge(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else "")
        for end in (e["source"], e["target"]):
            nodes.setdefault(end, {"id": end, "kind": "type" if end.startswith("t:") else "activity", "label": end[2:], "count": 0})
        edges.append({**e, "count": n})
    return {"nodes": list(nodes.values()), "edges": edges}


def diff(exp: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """One graph holding both, each node and edge with a status, and a summary in words."""
    en, rn = {n["id"]: n for n in exp["nodes"]}, {n["id"]: n for n in run["nodes"]}
    ee, re_ = {e["id"]: e for e in exp["edges"]}, {e["id"]: e for e in run["edges"]}
    nodes, edges = [], []
    for i in list(en) + [i for i in rn if i not in en]:
        n = {**en.get(i, {}), **rn.get(i, {})}
        n["count"] = rn.get(i, {}).get("count", 0)
        n["status"] = "matched" if i in en and i in rn else "missing" if i in en else "unexpected"
        nodes.append(n)
    for i in list(ee) + [i for i in re_ if i not in ee]:
        e = {**ee.get(i, {}), **re_.get(i, {})}
        e["count"] = re_.get(i, {}).get("count", 0)
        e["status"] = ("matched" if i in re_ else "conditional" if ee[i].get("conditional") else "missing") if i in ee else "unexpected"
        edges.append(e)
    # a node only reached through conditional edges is conditional too when it was never recorded
    cond_only = {n for e in edges if e["status"] == "conditional" for n in (e["source"], e["target"])} - \
                {n for e in edges if e["status"] in ("missing", "matched") and e.get("conditional") is False for n in (e["source"], e["target"])}
    for n in nodes:
        if n["status"] == "missing" and n["id"] in cond_only:
            n["status"] = "conditional"

    def words(e):
        s, t = e["source"][2:], e["target"][2:]
        if e["kind"] == "relation":
            return f"{s} {e['label'] or 'relates to'} {t}"
        return f"{t} used {s}" if e["kind"] == "used" else f"{s} {e['kind']} {t}"     # used: type -> activity
    by = Counter(e["status"] for e in edges)
    summary = {k: by.get(k, 0) for k in ("matched", "missing", "conditional", "unexpected")}
    lists = {k: [words(e) for e in edges if e["status"] == k][:50] for k in ("missing", "conditional", "unexpected")}
    lists["missing_types"] = [n["label"] for n in nodes if n["status"] == "missing" and n["kind"] == "type"]
    lists["unexpected_types"] = [n["label"] for n in nodes if n["status"] == "unexpected" and n["kind"] == "type"]
    return {"nodes": nodes, "edges": edges, "summary": summary, **lists,
            "clean": summary["missing"] == 0 and not lists["missing_types"]}
