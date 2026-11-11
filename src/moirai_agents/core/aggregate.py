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

import os
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
        nodes.setdefault(f"t:{t}", {"id": f"t:{t}", "kind": "type", "label": t, "role": roles.get(t, ""), "artifact": t in arts, "attrs": []})
        return f"t:{t}"

    for t in roles:
        tnode(t)
    for a in spec.get("activities", []):
        name = a.get("name")
        if not name:
            continue
        aid = f"a:{name}"
        nodes.setdefault(aid, {"id": aid, "kind": "activity", "label": name, "file": a.get("file", ""), "inputs": [], "outputs": []})
        cap = a.get("capture") if isinstance(a.get("capture"), dict) else {}
        nodes[aid]["inputs"] = sorted(set(nodes[aid]["inputs"]) | {str(k) for k in (cap.get("inputs") or {})})
        for k in ACT_EDGES:
            for ent in a.get(k, []) or []:
                t = (ent or {}).get("type")
                if not t:
                    continue
                e = _edge(k, tnode(t), aid) if k == "used" else _edge(k, aid, tnode(t))
                nodes[f"t:{t}"]["attrs"] = sorted(set(nodes[f"t:{t}"]["attrs"]) | {str(x) for x in (ent.get("attributes") or {})})
                cur = edges.setdefault(e["id"], {**e, "conditional": True})
                cur["conditional"] = cur["conditional"] and bool(ent.get("when"))      # unconditional once any declaration is unconditional
        for r in a.get("relations", []) or []:
            s, o = (r.get("subject") or {}).get("type"), (r.get("object") or {}).get("type")
            if s and o:
                e = _edge("relation", tnode(s), tnode(o), str(r.get("relation", "")))
                cur = edges.setdefault(e["id"], {**e, "conditional": True})
                cur["conditional"] = cur["conditional"] and bool(r.get("when"))
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


CAPTURED = (".input", ".output", ".attribute")      # the types prov_annotate's helper records (v7): one entity per value, uri name=value


TYPE_MAX = 31      # the trace keeps at most 31 characters of an entity type: "of3_template_alignment.attribute" is read back as "...attribut"


def ends(t: Any, *suffixes: str) -> bool:
    """True when the entity type t ends with one of the suffixes, also when the trace cut the type at TYPE_MAX characters."""
    if not isinstance(t, str):
        return False
    sfx = suffixes[0] if len(suffixes) == 1 and isinstance(suffixes[0], tuple) else suffixes
    if t.endswith(tuple(sfx)):
        return True
    i = t.rfind(".")
    return len(t) >= TYPE_MAX and i > 0 and len(t) - i >= 2 and any(s.startswith(t[i:]) for s in sfx)


def captured_type(t: Any) -> bool:
    return ends(t, CAPTURED)


def fold(g: Any) -> Dict[str, Dict[str, List[str]]]:
    """The captured values as properties of what they describe, not nodes of their own: {"attrs": {entity id: ["name=value"]} (attribute
    entities part_of it), "inputs": {activity id: [...]} (what it used of <activity>.input), "outputs": {activity id: [...]}}."""
    etype = {i: e.get("type", "") for i, e in g.entities.items()}
    uri = lambda i: (g.entities.get(i) or {}).get("uri", "") or i       # noqa: E731
    out: Dict[str, Dict[str, List[str]]] = {"attrs": {}, "inputs": {}, "outputs": {}}
    for r in g.relations:
        if ends(etype.get(r["subject"], ""), ".attribute") and r.get("object"):
            out["attrs"].setdefault(r["object"], []).append(uri(r["subject"]))
    for e in g.edges:
        t = etype.get(e["entity"], "")
        if ends(t, ".input") and e["kind"] == "used":
            out["inputs"].setdefault(e["activity"], []).append(uri(e["entity"]))
        elif ends(t, ".output") and e["kind"] == "generated":
            out["outputs"].setdefault(e["activity"], []).append(uri(e["entity"]))
    return out


def _names(values) -> List[str]:
    return sorted({v.split("=", 1)[0] for vs in values for v in vs if "=" in v})


def runtime(g: Any) -> Dict[str, Any]:
    """The aggregated graph of what was recorded (a provtrace.Graph), with counts. Captured values are not nodes: each entity type lists the
    attribute names its entities carry (attrs), each activity the names of its inputs and outputs."""
    roles = {t: (d or {}).get("role", "") for t, d in g.types.items()}
    etype = {i: e.get("type", "") or "?" for i, e in g.entities.items() if not captured_type(e.get("type", ""))}
    aname = {a["id"]: a["name"] for a in g.activities}
    fo = fold(g)
    tc = Counter(etype.values())
    ac = Counter(aname.values())
    nodes = {f"t:{t}": {"id": f"t:{t}", "kind": "type", "label": t, "role": roles.get(t, ""), "count": n,
                        "attrs": _names(v for i, v in fo["attrs"].items() if etype.get(i) == t)} for t, n in tc.items()}
    nodes.update({f"a:{n}": {"id": f"a:{n}", "kind": "activity", "label": n, "count": k,
                             "inputs": _names(v for i, v in fo["inputs"].items() if aname.get(i) == n),
                             "outputs": _names(v for i, v in fo["outputs"].items() if aname.get(i) == n)} for n, k in ac.items()})
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


def _captured(node_id: str) -> bool:
    return node_id.startswith("t:") and ends(node_id, CAPTURED)


def diff(exp: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """One graph holding both, each node and edge with a status, and a summary in words. Captured values (an activity's inputs and outputs,
    an entity's attributes) are recorded by the annotation helper itself, not promised type by type: their status is "captured"."""
    en, rn = {n["id"]: n for n in exp["nodes"]}, {n["id"]: n for n in run["nodes"]}
    ee, re_ = {e["id"]: e for e in exp["edges"]}, {e["id"]: e for e in run["edges"]}
    nodes, edges = [], []
    for i in list(en) + [i for i in rn if i not in en]:
        n = {**en.get(i, {}), **rn.get(i, {})}
        n["count"] = rn.get(i, {}).get("count", 0)
        for k in ("attrs", "inputs", "outputs"):           # the names the spec promised, and the ones recorded
            if en.get(i, {}).get(k) or rn.get(i, {}).get(k):
                n[k] = sorted(set(en.get(i, {}).get(k) or []) | set(rn.get(i, {}).get(k) or []))
        n["status"] = "matched" if i in en and i in rn else "missing" if i in en else "captured" if _captured(i) else "unexpected"
        nodes.append(n)
    for i in list(ee) + [i for i in re_ if i not in ee]:
        e = {**ee.get(i, {}), **re_.get(i, {})}
        e["count"] = re_.get(i, {}).get("count", 0)
        e["status"] = ("matched" if i in re_ else "conditional" if ee[i].get("conditional") else "missing") if i in ee else \
            "captured" if _captured(e["source"]) or _captured(e["target"]) else "unexpected"
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
    summary = {k: by.get(k, 0) for k in ("matched", "missing", "conditional", "unexpected", "captured")}
    lists = {k: [words(e) for e in edges if e["status"] == k][:50] for k in ("missing", "conditional", "unexpected")}
    lists["missing_types"] = [n["label"] for n in nodes if n["status"] == "missing" and n["kind"] == "type"]
    lists["unexpected_types"] = [n["label"] for n in nodes if n["status"] == "unexpected" and n["kind"] == "type"]
    return {"nodes": nodes, "edges": edges, "summary": summary, **lists,
            "clean": summary["missing"] == 0 and not lists["missing_types"]}


# ------------------------------------------------------------------ the same aggregated graph, computed by TraceViewer (no graph in Python)
def _kind(variable: str) -> str:
    """args.relations.<kind>.<i> -> <kind>"""
    parts = str(variable).split(".")
    return parts[2] if len(parts) > 2 else str(variable)


def _n(lf: Any, colname: str) -> int:
    """Rows of a (lazy) frame with a value in colname."""
    r = lf.group_by(None, f"count_valid:{colname}:n").collect().to_dicts()
    return int(r[0]["n"]) if r else 0


def _cats(tv: Any, col: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in tv.filter(col("ph") != 4).group_by("cat").agg(col("name").count()).collect().to_dicts():
        out[str(r.get("cat"))] = int(next((v for k, v in r.items() if k != "cat"), 0) or 0)
    return out


def tv_partial(args: tuple) -> Dict[str, Any]:
    """One trace file through TraceViewer (run in a worker process): what tv_runtime merges. Everything that is local to the file is grouped
    by the engine; what may be shared with other files comes back as keys: entities (id, type, uri name) and entity relations (relation,
    subject, object) for the parent to dedup, and activity relations to entities this file does not declare, to resolve there."""
    path, idx = args
    from . import provtrace as T
    U = T._tv()
    col = U.col
    base = U.TraceViewer([path], idx)
    rel = sorted(c for c in base.columns if c.startswith("args.relations."))
    tv = base.phase("any")
    rows = lambda lf: lf.collect().to_dicts()       # noqa: E731
    out: Dict[str, Any] = {"types": {}, "cats": _cats(tv, col), "kinds": {}, "acts": {}, "edges": 0, "ents": [], "eg": [], "loose": [], "ers": []}
    meta_df = tv.filter(col("name").is_in(list(T.PROV_NAMES))).collect()
    meta = meta_df.lazy()
    kinds = out["kinds"] = {r["name"]: r["n"] for r in rows(meta.group_by(["name"], "count_valid:name:n"))} if meta_df.height else {}
    if kinds.get("ET"):
        for r in rows(meta.filter(col("name") == "ET")):
            t, role = r.get("args.type"), r.get("args.role", 0)
            if t and t not in out["types"]:
                out["types"][t] = T.ROLES[role] if isinstance(role, int) and 0 <= role < len(T.ROLES) else str(role)
    ents = None
    if kinds.get("EH"):
        ents = (meta.filter(col("name") == "EH").select("args.id", "args.type", "args.uri")
                .with_columns(**{"args.id": col("args.id").cast("string")}).filter(col("args.id").is_not_null()).unique("args.id")
                .with_columns(an=col("args.uri").extract(r"^([^=]*)=")).select("args.id", "args.type", "an").collect().lazy())
        out["ents"] = [(r["args.id"], r["args.type"] or "?", r["an"]) for r in rows(ents)]
    acts_df = tv.filter(col("cat").is_in(list(T.ACTIVITY_CATS)) & (col("ph") == 1)).select("name", "pid", "ts", *rel).collect()
    if acts_df.height:
        acts = acts_df.lazy()
        if _n(acts, "ts") != _n(acts.group_by(["pid", "ts"], "count_valid:ts:n"), "n"):
            acts = acts.unique(["pid", "ts"]).collect().lazy()          # one activity per (pid, ts); unique() over the wide columns is slow: only when needed
        out["acts"] = {r["name"]: r["n"] for r in rows(acts.group_by(["name"], "count_valid:ts:n"))}
        if rel:
            long_df = (acts.select("name", *rel).with_columns(**{c: col(c).cast("string") for c in rel})
                       .unpivot(id_vars=["name"], value_vars=rel).filter(col("value").is_not_null()).collect())
            if long_df.height:
                long = long_df.lazy()
                out["edges"] = long_df.height
                if ents is not None:
                    out["eg"] = [(r["variable"], r["name"], r["args.type"] or "?", r["an"], r["n"]) for r in
                                 rows(long.join(ents, left_on="value", right_on="args.id", how="inner")
                                      .group_by(["variable", "name", "args.type", "an"], "count_valid:value:n", dropna=False))]
                    loose = long.join(ents, left_on="value", right_on="args.id", how="anti").collect()
                else:
                    loose = long_df
                if loose.height:
                    out["loose"] = [(r["variable"], r["name"], r["value"], r["n"]) for r in
                                    rows(loose.lazy().group_by(["variable", "name", "value"], "count_valid:value:n"))]
    if kinds.get("ER"):
        ers = (meta.filter(col("name") == "ER").select("args.relation", "args.subject", "args.object")
               .with_columns(**{"args.subject": col("args.subject").cast("string"), "args.object": col("args.object").cast("string")})
               .filter(col("args.subject").is_not_null()).unique(["args.relation", "args.subject", "args.object"]))
        out["ers"] = [(r["args.relation"], r["args.subject"], r["args.object"] or "") for r in rows(ers)]
    return out


def tv_runtime(traces: str, workers: int = 0) -> Dict[str, Any]:
    """runtime() of the traces and their counts through dftracer-utils' TraceViewer: {nodes, edges, counts}. The index is built on all cores,
    then each file is read and grouped by the engine in its own process (tv_partial), and the parent merges: entities and entity relations
    dedup across files by id / key, as one graph would, and an activity relation to an entity declared in another file is resolved there.
    Equal to runtime(provtrace.read(traces)) with g.counts(), without building the graph in Python (millions of entities for a multi-node run)."""
    from . import provtrace as T
    files = T.expand(traces)
    if not files:
        return {"nodes": [], "edges": [], "counts": T.Graph().counts()}
    idx = T.ensure_index(files)
    n = workers or min(len(files), os.cpu_count() or 4, int(os.environ.get("MOIRAI_TV_WORKERS", "48")))
    if n < 2 or len(files) < 2:
        parts = [tv_partial((f, idx)) for f in files]
    else:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn")) as ex:     # spawn: TraceViewer's threads are running here
            parts = list(ex.map(tv_partial, [(f, idx) for f in files]))

    types: Dict[str, str] = {}
    cats: Counter = Counter()
    kinds: Counter = Counter()
    ac: Counter = Counter()
    ent: Dict[str, tuple] = {}
    edge_n = 0
    for p in parts:
        for t, r in p["types"].items():
            types.setdefault(t, r)
        cats.update(p["cats"])
        kinds.update(p["kinds"])
        ac.update(p["acts"])
        edge_n += p["edges"]
        for i, t, a in p["ents"]:
            if i not in ent:
                ent[i] = (t, a)
    tc = Counter(t for t, _ in ent.values())
    eg: Counter = Counter()
    dangling: set = set()
    for p in parts:
        for v, a, t, an, k in p["eg"]:
            eg[(v, a, t, an)] += k
        for v, a, i, k in p["loose"]:
            if i in ent:
                eg[(v, a) + ent[i]] += k
            else:
                dangling.add(i)
    rg: Counter = Counter()
    seen: set = set()
    for p in parts:
        for key in p["ers"]:
            if key in seen:
                continue
            seen.add(key)
            rv, s, o = key
            for x in (s, o):
                if x and x not in ent:
                    dangling.add(x)
            if s in ent and o in ent:
                rg[(rv, ent[s][0], ent[o][0], ent[s][1])] += 1

    nodes = {f"t:{t}": {"id": f"t:{t}", "kind": "type", "label": t, "role": types.get(t, ""), "count": k, "attrs": []}
             for t, k in tc.items() if not captured_type(t)}
    attrs: Dict[str, set] = {}
    for (rv, st, ot, san), k in rg.items():
        if ends(st, ".attribute") and san:
            attrs.setdefault(ot, set()).add(san)
    for t, names in attrs.items():
        if f"t:{t}" in nodes:
            nodes[f"t:{t}"]["attrs"] = sorted(names)
    io: Dict[str, Dict[str, set]] = {}
    ec: Counter = Counter()
    for (v, a, t, an), k in eg.items():
        kd = _kind(v)
        if captured_type(t):
            if an and ((kd == "used" and ends(t, ".input")) or (kd == "generated" and ends(t, ".output"))):
                io.setdefault(a, {"inputs": set(), "outputs": set()})["inputs" if kd == "used" else "outputs"].add(an)
            continue
        e = _edge(kd, f"t:{t}", f"a:{a}") if kd == "used" else _edge(kd, f"a:{a}", f"t:{t}")
        ec[e["id"]] += k
    for (rv, st, ot, san), k in rg.items():
        if captured_type(st) or captured_type(ot):
            continue
        ec[_edge("relation", f"t:{st}", f"t:{ot}", T.ENTITY_RELATIONS.get(rv, str(rv)))["id"]] += k
    nodes.update({f"a:{a}": {"id": f"a:{a}", "kind": "activity", "label": a, "count": k,
                             "inputs": sorted((io.get(a) or {}).get("inputs", ())), "outputs": sorted((io.get(a) or {}).get("outputs", ()))}
                  for a, k in ac.items()})
    edges = []
    for eid, k in ec.items():
        parts_ = eid.split("|")
        e = _edge(parts_[0], parts_[1], parts_[2], parts_[3] if len(parts_) > 3 else "")
        for end in (e["source"], e["target"]):
            nodes.setdefault(end, {"id": end, "kind": "type" if end.startswith("t:") else "activity", "label": end[2:], "count": 0})
        edges.append({**e, "count": k})
    other = {k: v for k, v in cats.items() if k not in T.ACTIVITY_CATS}
    if other.get("dftracer"):
        other["dftracer"] = max(0, other["dftracer"] - sum(kinds.values()))
    counts = {"activities": sum(ac.values()), "entities": len(ent), "edges": edge_n, "relations": len(seen), "types": len(types),
              "records": sum(cats.values()), "other_events": {k: v for k, v in other.items() if v}, "dangling": len(dangling)}
    return {"nodes": list(nodes.values()), "edges": edges, "counts": counts}
