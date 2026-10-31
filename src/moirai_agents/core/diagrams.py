"""Diagrams a pipeline shows in the web UI, kept as pipeline outputs beside the provenance work.

One place for what the capability publishes and what the web server serves and saves, so the two cannot drift.
Layout: <provenance>/diagrams/index.json  [{name, stage, title, summary, source, kind, nodes, edges, bytes, ts, edited_ts}]
        <provenance>/diagrams/<name>.json         the diagram as published (graph: Cytoscape-style nodes/edges; bokeh: a bokeh json_item)
        <provenance>/diagrams/<name>.edited.json  the user's edited copy of a graph (the original is never overwritten)
A graph is {nodes: [{id, label, parent?, kind?, info?}], edges: [{id?, source, target, label?, kind?, conditional?}], legend?: [{kind, label}]}.
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import pipeline as P

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
MAX_NODES, MAX_EDGES, MAX_TEXT, MAX_BYTES = 800, 4000, 240, 3_000_000
KINDS = ("graph", "bokeh")


def graph_problems(g: Any) -> List[Tuple[str, str, str]]:
    """Faults that would break the page or mislead: [(check, detail, fix)]. Empty when the graph can be drawn."""
    if not isinstance(g, dict) or not isinstance(g.get("nodes"), list) or not isinstance(g.get("edges"), list):
        return [("shape", "a graph is {nodes: [...], edges: [...]}", "build it with nodes and edges lists")]
    out: List[Tuple[str, str, str]] = []
    nodes, edges = g["nodes"], g["edges"]
    if not nodes:
        return [("not_empty", "the graph has no nodes", "add the entities and activities to draw")]
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        out.append(("size", f"{len(nodes)} nodes / {len(edges)} edges, over {MAX_NODES} / {MAX_EDGES}", "group or collapse repeated nodes, or split the diagram"))
    ids, bad = set(), []
    for n in nodes:
        i = n.get("id") if isinstance(n, dict) else None
        if not isinstance(i, str) or not i or len(i) > 160 or i in ids:
            bad.append(i)
        else:
            ids.add(i)
    if bad:
        out.append(("node_ids", f"missing, repeated or too long node ids: {[str(b)[:40] for b in bad[:5]]}", "give every node a unique string id (at most 160 characters)"))
    parents = {n["id"]: n.get("parent") for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}
    miss = sorted({str(p) for p in parents.values() if p and p not in ids})
    if miss:
        out.append(("parents", f"parent nodes that do not exist: {miss[:5]}", "add the parent node or drop 'parent'"))
    for start in parents:
        seen, cur = set(), start
        while cur and cur in parents and cur not in seen:
            seen.add(cur)
            cur = parents.get(cur)
        if cur and cur in seen:
            out.append(("parent_cycle", f"'{start}' is its own ancestor", "break the parent chain"))
            break
    dangling = [(e.get("source"), e.get("target")) for e in edges if not isinstance(e, dict) or e.get("source") not in ids or e.get("target") not in ids]
    if dangling:
        out.append(("edge_ends", f"{len(dangling)} edges name a node that does not exist, e.g. {dangling[:3]}", "fix the edge ends, or add the nodes"))
    long = [n.get("id") for n in nodes if isinstance(n, dict) and len(str(n.get("label", ""))) > MAX_TEXT]
    if long:
        out.append(("labels", f"labels over {MAX_TEXT} characters on {long[:3]}", "shorten the labels; put detail in 'info'"))
    if len(json.dumps(g)) > MAX_BYTES:
        out.append(("bytes", f"over {MAX_BYTES} bytes", "split the diagram"))
    return out


def normalize(g: Dict[str, Any]) -> Dict[str, Any]:
    """A clean copy: string labels, edge ids, boolean flags. Call after graph_problems found nothing."""
    nodes = [{k: v for k, v in n.items() if k in ("id", "label", "parent", "kind", "info", "x", "y", "group")} for n in g["nodes"]]
    for n in nodes:
        n["label"] = str(n.get("label", n["id"]))[:MAX_TEXT]
    edges = []
    for i, e in enumerate(g["edges"]):
        edges.append({"id": str(e.get("id") or f"e{i}"), "source": e["source"], "target": e["target"], "label": str(e.get("label", ""))[:MAX_TEXT],
                      "kind": str(e.get("kind") or "flow"), "conditional": bool(e.get("conditional"))})
    out = {"nodes": nodes, "edges": edges}
    if isinstance(g.get("legend"), list):
        out["legend"] = [{"kind": str(x.get("kind")), "label": str(x.get("label", ""))[:80]} for x in g["legend"] if isinstance(x, dict)]
    return out


def bokeh_problems(item: Any) -> List[Tuple[str, str, str]]:
    """Faults in a bokeh json_item: [(check, detail, fix)]. The page serves BokehJS from the installed bokeh, so the versions must agree."""
    if not isinstance(item, dict) or not {"doc", "target_id", "root_id"} <= set(item):
        return [("shape", "not a bokeh.embed.json_item result (needs doc, target_id, root_id)", "build it with bokeh.embed.json_item(figure)")]
    out: List[Tuple[str, str, str]] = []
    try:
        import bokeh
        have, want = bokeh.__version__.split(".")[:2], str(item.get("version", "")).split(".")[:2]
        if want and want != [""] and have != want:
            out.append(("version", f"item made by bokeh {item.get('version')}, the server has {bokeh.__version__}", "rebuild the item with the installed bokeh"))
    except ImportError:
        out.append(("bokeh", "bokeh is not installed in the server's Python", "pip install bokeh in the moirai-agents environment"))
    if len(json.dumps(item)) > MAX_BYTES:
        out.append(("bytes", f"over {MAX_BYTES} bytes", "plot fewer points or aggregate them"))
    return out


def diff(original: Dict[str, Any], edited: Dict[str, Any]) -> Dict[str, Any]:
    """What the user changed in a graph: removed / added nodes and edges, renamed labels. Edges compare by (source, target, kind)."""
    groups = {n.get("parent") for n in original["nodes"] if n.get("parent")}        # a parent is a group, drawn as a colour; an edit does not carry it
    on, en = {n["id"]: n for n in original["nodes"] if n["id"] not in groups}, {n["id"]: n for n in edited["nodes"]}
    ok = {(e["source"], e["target"], e.get("kind", "flow")): e for e in original["edges"] if e["source"] not in groups and e["target"] not in groups}
    ek = {(e["source"], e["target"], e.get("kind", "flow")): e for e in edited["edges"]}
    return {"removed_nodes": [on[i]["label"] for i in on if i not in en], "added_nodes": [en[i]["label"] for i in en if i not in on],
            "renamed": [{"id": i, "from": on[i]["label"], "to": en[i]["label"]} for i in on if i in en and on[i]["label"] != en[i]["label"]],
            "removed_edges": [f"{on.get(k[0], {}).get('label', k[0])} -{k[2]}-> {on.get(k[1], {}).get('label', k[1])}" for k in ok if k not in ek],
            "added_edges": [f"{en.get(k[0], {}).get('label', k[0])} -{k[2]}-> {en.get(k[1], {}).get('label', k[1])}" for k in ek if k not in ok]}


def _dir(c: Dict[str, Any]) -> Path:
    return P.work(c) / "diagrams"


def _index(c: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        v = json.loads((_dir(c) / "index.json").read_text())
    except (OSError, ValueError):
        return []
    return v if isinstance(v, list) else []


def listing(c: Dict[str, Any], stage: Optional[str] = None) -> List[Dict[str, Any]]:
    """The published diagrams of a pipeline (of one stage when given), oldest first, without their content."""
    return [e for e in _index(c) if not stage or e.get("stage") == stage]


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read(c: Dict[str, Any], name: str) -> Dict[str, Any]:
    """One diagram with its content: {..entry, graph, edited, changes} for a graph, {..entry, item} for a bokeh plot. Raises P.PipelineError when there is none."""
    e = next((x for x in _index(c) if x.get("name") == name), None) if NAME.match(name or "") else None
    if e is None:
        raise P.PipelineError(f"no diagram {name!r} in {c['id']}; published: {[x.get('name') for x in _index(c)]}")
    body = _load(_dir(c) / f"{name}.json")
    if body is None:
        raise P.PipelineError(f"diagram {name!r} is indexed but its file is missing or unreadable")
    if e.get("kind") == "bokeh":
        return {**e, "item": body}
    edited = _load(_dir(c) / f"{name}.edited.json")
    return {**e, "graph": body, "edited": edited, "changes": diff(body, edited) if edited else None}


def _publish(pid: str, stage: str, name: str, title: str, kind: str, body: Dict[str, Any], counts: Dict[str, int], summary: str, source: str, workspace: Optional[str] = None) -> Dict[str, Any]:
    c = P.load(pid, workspace)
    if stage not in c["stages"]:
        raise P.PipelineError(f"stage must be one of {list(c['stages'])}")
    if not NAME.match(name or ""):
        raise P.PipelineError("name must be short: letters, digits, . _ - (it becomes a file name)")
    d = _dir(c)
    d.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body)
    _atomic(d / f"{name}.json", text)
    (d / f"{name}.edited.json").unlink(missing_ok=True)       # a new version of the diagram starts without the old edits
    entry = {"name": name, "stage": stage, "title": (title or name)[:120], "summary": (summary or "")[:400], "source": (source or "")[:300], "kind": kind,
             **counts, "bytes": len(text.encode()), "ts": round(time.time(), 1)}
    idx = [e for e in _index(c) if e.get("name") != name] + [entry]
    _atomic(d / "index.json", json.dumps(idx, indent=1))
    P.record(pid, stage, outputs={"diagrams": [e["name"] for e in idx if e.get("stage") == stage]})      # a pipeline output, kept for later; presentation only
    return entry


def publish_graph(pid: str, stage: str, name: str, title: str, graph: Dict[str, Any], summary: str = "", source: str = "", workspace: Optional[str] = None) -> Dict[str, Any]:
    """Save a graph diagram for a stage (same name replaces it). Raises P.PipelineError on a fault in the graph, the name or the stage."""
    bad = graph_problems(graph)
    if bad:
        raise P.PipelineError("; ".join(f"{n}: {d} ({f})" for n, d, f in bad))
    g = normalize(graph)
    return _publish(pid, stage, name, title, "graph", g, {"nodes": len(g["nodes"]), "edges": len(g["edges"])}, summary, source, workspace)


def publish_bokeh(pid: str, stage: str, name: str, title: str, item: Dict[str, Any], summary: str = "", source: str = "", workspace: Optional[str] = None) -> Dict[str, Any]:
    """Save an interactive Bokeh plot (a bokeh.embed.json_item result) for a stage. Raises P.PipelineError on a fault."""
    bad = bokeh_problems(item)
    if bad:
        raise P.PipelineError("; ".join(f"{n}: {d} ({f})" for n, d, f in bad))
    return _publish(pid, stage, name, title, "bokeh", item, {}, summary, source, workspace)


def save_edit(pid: str, name: str, graph: Dict[str, Any], workspace: Optional[str] = None) -> Dict[str, Any]:
    """Keep the user's edited copy of a graph beside the original. Returns {entry, changes}. Raises P.PipelineError on a fault or an unknown graph."""
    c = P.load(pid, workspace)
    d = read(c, name)
    if d.get("kind") != "graph":
        raise P.PipelineError("only graphs can be edited")
    bad = graph_problems(graph)
    if bad:
        raise P.PipelineError("; ".join(f"{n}: {x} ({f})" for n, x, f in bad))
    g = normalize(graph)
    _atomic(_dir(c) / f"{name}.edited.json", json.dumps(g))
    idx = [{**e, "edited_ts": round(time.time(), 1)} if e.get("name") == name else e for e in _index(c)]
    _atomic(_dir(c) / "index.json", json.dumps(idx, indent=1))
    return {"entry": next(e for e in idx if e.get("name") == name), "changes": diff(d["graph"], g)}


def reset_edit(pid: str, name: str, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Drop the user's edited copy and go back to the published graph."""
    c = P.load(pid, workspace)
    read(c, name)
    (_dir(c) / f"{name}.edited.json").unlink(missing_ok=True)
    idx = [{k: v for k, v in e.items() if k != "edited_ts"} if e.get("name") == name else e for e in _index(c)]
    _atomic(_dir(c) / "index.json", json.dumps(idx, indent=1))
    return {"name": name, "edited": False}


def from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The graph of an annotation spec (the spec.json annotate_spec saves): entity types and activities as nodes, activities grouped under their source file,
    edges used / generated / invalidated / updated and entity relations, conditional ones flagged. Every count comes from the spec."""
    arts = set(spec.get("scientific_artifacts") or [])
    et = {e["type"]: e for e in spec.get("entity_types", []) if isinstance(e, dict) and e.get("type")}
    nodes, edges, seen_t, files = [], [], set(), []
    for a in spec.get("activities", []):
        f = a.get("file") or "(no file)"
        if f not in files:
            files.append(f)

    def tnode(t):
        if t in seen_t:
            return
        seen_t.add(t)
        e = et.get(t, {})
        nodes.append({"id": f"t:{t}", "label": ("★ " if t in arts else "") + t, "kind": "artifact" if t in arts else (e.get("role") or "entity"),
                      "info": {"kind": "entity type", "role": e.get("role", "?"), "scientific artifact": "yes" if t in arts else "no",
                               "id scheme": e.get("id_scheme", ""), "meaning": e.get("description", "")}})

    for f in files:
        nodes.append({"id": f"f:{f}", "label": f.rsplit("/", 1)[-1], "kind": "file", "info": {"kind": "source file", "path": f}})
    for t in et:
        tnode(t)
    for i, a in enumerate(spec.get("activities", [])):
        mode = a.get("mode") or "wrap"
        where = a.get("function") or a.get("target") or (f"{a.get('in', '')}: {a.get('anchor', '')}" if a.get("anchor") else a.get("in", ""))
        aid = f"a:{a.get('name', i)}"
        nodes.append({"id": aid, "label": a.get("name", f"activity {i}"), "parent": f"f:{a.get('file') or '(no file)'}", "kind": "activity",
                      "info": {"kind": "activity", "attached by": mode, "where": where, "file": a.get("file", "")}})
        for k in ("used", "generated", "invalidated", "updated"):
            for ent in a.get(k, []):
                t = ent.get("type")
                if not t:
                    continue
                tnode(t)
                src, dst = (f"t:{t}", aid) if k == "used" else (aid, f"t:{t}")
                edges.append({"source": src, "target": dst, "kind": k, "label": k if ent.get("when") or k in ("invalidated", "updated") else "",
                              "conditional": bool(ent.get("when")), **({"info": ent["when"]} if ent.get("when") else {})})
        for r in a.get("relations", []):
            s, o = (r.get("subject") or {}).get("type"), (r.get("object") or {}).get("type")
            if s and o:
                tnode(s)
                tnode(o)
                edges.append({"source": f"t:{s}", "target": f"t:{o}", "kind": "relation", "label": str(r.get("relation", "")), "conditional": bool(r.get("when"))})
    uniq, seen = [], set()
    for e in edges:                       # the same ends, kind and condition are drawn once; the count is kept in the label
        key = (e["source"], e["target"], e["kind"], e["conditional"], e["label"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append({k: v for k, v in e.items() if k != "info"})
    return {"nodes": nodes, "edges": uniq, "legend": [{"kind": k, "label": v} for k, v in (
        ("used", "activity used it"), ("generated", "activity made it"), ("invalidated", "activity deleted it"), ("updated", "activity changed it"),
        ("relation", "relation between entities"), ("artifact", "scientific artifact (★)"), ("conditional", "only if a condition holds (dashed)"))]}
