"""Evidence-backed answers to provenance questions (Moirai goal 3: query plan -> evidence -> synthesis).

The agent writes the plan, collects evidence with the query tools and writes the answer; ``publish`` checks it against the
traces deterministically before anyone sees it:
  - every entity and activity the evidence cites exists in the traces,
  - every target resolves, and the lineage shown is computed here (Graph.lineage), never taken from the agent,
  - entities named sensitive are followed forward: everything derived from them is the sensitive lineage (restrict or
    review before sharing).
An answer that fails a check is still recorded, but marked unverified: the page then shows only the deterministic
lineage and the evidence (the fallback), never the unverified summary as if it were checked.
Answers are kept per pipeline in answers.jsonl - the audit trail.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import pipeline as P
from . import provtrace as T

MAX_NODES = 80
_LOCK = threading.Lock()


def traces_of(c: Dict[str, Any]) -> str:
    if c["kind"] == "analysis":
        return c["traces"]
    if c["kind"] == "live":
        return P.stage(c, "live_run")["outputs"].get("traces") or (c.get("dashboard") or {}).get("traces") or ""
    o = P.stage(c, "baseline")["outputs"]
    return o.get("traces") or (c.get("dashboard") or {}).get("traces") or P.stage(c, "moirai_smoke")["outputs"].get("traces", "")


class _Reader:
    """One pipeline's graph, kept and extended: a live campaign's traces grow, and only what is new is read."""
    def __init__(self, traces: str) -> None:
        self.traces, self.tail, self.g, self.sizes = traces, T.Tail(traces), T.Graph(), {}
        self.lock = threading.Lock()


_READERS: Dict[str, _Reader] = {}


def graph(c: Dict[str, Any]) -> Tuple["T.Graph", List[str]]:
    """The provenance graph of a pipeline's traces. Read incrementally: the first call reads everything, later calls only
    what was appended since (a file that shrank, or new traces, starts over). Shared by the query tools, answer_publish,
    the graph stage and the Query view."""
    t = traces_of(c)
    if not t:
        return T.Graph(), []
    files = T.expand(t)
    sizes = {f: os.path.getsize(f) for f in files if os.path.exists(f)}
    with _LOCK:
        r = _READERS.get(c["id"])
        if r is None or r.traces != t or any(sizes.get(f, 0) < n for f, n in r.sizes.items()):
            r = _READERS[c["id"]] = _Reader(t)
    with r.lock:
        if sizes != r.sizes:
            while _drain(r):
                pass
            r.sizes = sizes
    return r.g, files


def _drain(r: _Reader) -> bool:
    recs = r.tail.poll()
    if recs:
        r.g.extend(recs)
    r.g.set_files(r.tail.files)
    return bool(recs)


def _resolve(g: "T.Graph", ref: str) -> List[str]:
    ref = str(ref or "").strip()
    return g.find(ref) if ref else []


def _subgraph(g: "T.Graph", starts: List[str], direction: str) -> Dict[str, Any]:
    """Nodes and edges of the lineage from ``starts`` (both directions = 'both'), at most MAX_NODES nodes."""
    ents, acts, edges = set(starts), {}, []
    for d in (["back", "forward"] if direction == "both" else [direction]):
        for s in starts:
            r = g.lineage(s, d, limit=MAX_NODES)
            if r.get("status") != "ok":
                continue
            for st in r["steps"]:
                aid = f"a:{st['activity']}:{st['hop']}:{st['from']}"
                acts[aid] = st["activity"]
                for x in st["to"]:
                    ents.add(x)
                if d == "back":
                    edges += [(aid, st["from"], "generated")] + [(x, aid, "used") for x in st["to"]]
                else:
                    edges += [(st["from"], aid, "used")] + [(aid, x, "generated") for x in st["to"]]
            if len(ents) + len(acts) > MAX_NODES:
                break
    nodes = [{"id": e, "kind": "entity", "type": g.entities.get(e, {}).get("type", ""), "label": _label(g, e), "start": e in starts}
             for e in list(ents)[:MAX_NODES]]
    keep = {n["id"] for n in nodes}
    nodes += [{"id": a, "kind": "activity", "label": n} for a, n in list(acts.items())[:MAX_NODES]]
    keep |= set(acts)
    return {"nodes": nodes, "edges": [{"source": a, "target": b, "kind": k} for a, b, k in edges if a in keep and b in keep],
            "truncated": len(ents) + len(acts) > MAX_NODES}


def _label(g: "T.Graph", eid: str) -> str:
    e = g.entities.get(eid) or {}
    uri = e.get("uri") or eid
    return (uri.rsplit("/", 1)[-1] or uri)[:40]


def publish(pipeline: str, question: str, plan: Dict[str, Any], evidence: List[Dict[str, Any]], answer: str,
            targets: List[str], direction: str = "back", gaps: Optional[List[str]] = None, confidence: float = 0.0,
            sensitive: Optional[List[str]] = None, chat: str = "") -> Dict[str, Any]:
    c = P.load(pipeline)
    g, files = graph(c)
    problems: List[str] = []
    names = {a["name"] for a in g.activities}
    cited_e, cited_a = set(), set()
    for i, ev in enumerate(evidence or []):
        if not isinstance(ev, dict):
            problems.append(f"evidence #{i + 1} is not an object")
            continue
        if ev.get("entity"):
            hit = _resolve(g, ev["entity"])
            if not hit:
                problems.append(f"evidence #{i + 1}: entity {ev['entity']!r} is not in the traces")
            cited_e.update(hit[:20])
        if ev.get("activity"):
            if ev["activity"] not in names:
                problems.append(f"evidence #{i + 1}: activity {ev['activity']!r} is not in the traces")
            cited_a.add(ev["activity"])
    starts: List[str] = []
    for t in targets or []:
        hit = _resolve(g, t)
        if not hit:
            problems.append(f"target {t!r} is not in the traces")
        starts += hit[:5]
    if not (evidence or []):
        problems.append("no evidence: every answer cites the records it rests on")
    if direction not in ("back", "forward", "both"):
        direction = "back"
    lineage = _subgraph(g, starts[:5], direction) if starts else {"nodes": [], "edges": [], "truncated": False}
    sens_ids: List[str] = []
    for s in sensitive or []:
        sens_ids += _resolve(g, s)
    sens_down = set(sens_ids)
    for s in sens_ids[:50]:
        r = g.lineage(s, "forward", limit=2000)
        if r.get("status") == "ok":
            sens_down.update(x for st in r["steps"] for x in st["to"])
    if sensitive and not sens_ids:
        problems.append(f"none of the sensitive references {sensitive} is in the traces")
    reached = {n["id"] for n in lineage["nodes"] if n["kind"] == "entity"} | cited_e
    acts_ts = [a["ts"] for a in g.activities if (a["name"] in cited_a or not cited_a) and isinstance(a.get("ts"), (int, float))]
    from . import querystore as QS
    rec = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 1), "pipeline": pipeline, "chat": chat, "question": question, "asked_by": QS.who(),
           "plan": plan or {}, "evidence": evidence or [], "answer": answer or "", "targets": targets or [], "direction": direction,
           "gaps": gaps or [], "confidence": max(0.0, min(1.0, float(confidence or 0))), "verified": not problems,
           "problems": problems, "lineage": lineage,
           "sensitive": {"named": sens_ids[:200], "lineage": sorted(sens_down)[:2000], "count": len(sens_down)} if sensitive else None,
           "stats": {"sources": len(files), "artifacts": len(reached), "activities": len(cited_a),
                     "window": [min(acts_ts) / 1e6, max(acts_ts) / 1e6] if acts_ts else None,
                     "records": g.total}}
    with open(P.pdir(pipeline) / "answers.jsonl", "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    QS.save(pipeline, rec)                 # kept with the trace too, for everyone who reads it
    return rec


def listing(pipeline: str, workspace: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """The pipeline's answers and every query kept with its traces (asked by anyone, through any pipeline on the same traces), oldest first.
    Each carries asked_by and category; one kept only in the shared store also carries stored_file."""
    f = P.pdir(pipeline, workspace) / "answers.jsonl"
    try:
        rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        rows = []
    last, order = {}, []                 # an answer updated later (its synthesis added) is appended again: keep its latest copy, in first order
    for r in rows:
        k = r.get("id") or id(r)
        if k not in last:
            order.append(k)
        last[k] = r
    from . import querystore as QS
    try:
        kept = QS.stored(traces_of(P.load(pipeline, workspace)))
    except (P.PipelineError, OSError, KeyError):
        kept = []
    by_id = {d["id"]: d for d in kept}
    for r in last.values():
        d = by_id.get(r.get("id"))
        r.setdefault("asked_by", (d or {}).get("asked_by") or "")
        r["category"] = QS.category(r)
    for d in kept:
        if d["id"] not in last:
            last[d["id"]] = {**d["record"], "asked_by": d.get("asked_by", ""), "category": QS.category(d["record"]),
                             "stored_file": d.get("_file", "")}
            order.append(d["id"])
    out = sorted((last[k] for k in order), key=lambda r: float(r.get("ts") or 0))[-limit:]
    from . import evidence as E            # what a computed answer's write-up says is not recorded is a gap too (older records included)
    for r in out:
        if r.get("engine") == "traceviewer" and r.get("synthesis") and isinstance(r.get("gaps"), list):
            r["gaps"] = r["gaps"] + E.reported_gaps(r["synthesis"], len(r.get("steps") or []), r["gaps"])
    return out


def pin(pipeline: str, aid: str, on: bool, workspace: Optional[str] = None) -> bool:
    f = P.pdir(pipeline, workspace) / "pins.json"
    try:
        pins = set(json.loads(f.read_text()))
    except (OSError, ValueError):
        pins = set()
    (pins.add if on else pins.discard)(aid)
    f.write_text(json.dumps(sorted(pins)))
    return on


def pins(pipeline: str, workspace: Optional[str] = None) -> List[str]:
    try:
        return json.loads((P.pdir(pipeline, workspace) / "pins.json").read_text())
    except (OSError, ValueError):
        return []
