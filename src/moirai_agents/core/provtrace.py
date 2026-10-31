"""Read moirai traces, also while the service is writing them.

The node trace is a sequence of independent gzip members of whole lines (dftracer ``design_pc_service``); the
service seals a partial member at least every ``DFTRACER_PC_FLUSH_MS``, and writes each at a reserved offset, so
for a moment a member can be missing before a later one is complete. :class:`Tail` therefore commits its position
only at the end of a whole member and simply retries from there next time: an unfinished member or a hole is never
misread. The same class reads finished traces, in one pass. Validators and queries use it; the live view is dfdashboard's.
"""
from __future__ import annotations

import glob
import json
import os
import zlib
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROLES = ["unknown", "input", "output", "intermediate", "parameter", "reference"]
STORES = ["memory", "gpu_memory", "local_disk", "parallel_fs", "burst_buffer", "object_store", "database", "network", "other"]
ENTITY_RELATIONS = {16: "derived_from", 17: "revision_of", 18: "contains", 19: "part_of", 20: "specialization_of",
                    21: "alternate_of", 22: "depends_on"}
ACTIVITY_CATS = ("moirai", "PROV")
READ_LIMIT = 8 << 20


def expand(traces: str) -> List[str]:
    """A directory, a glob or a file -> the .pfw/.pfw.gz files in it (spill files are traces too)."""
    t = os.path.expanduser(traces)
    if os.path.isdir(t):
        return sorted(glob.glob(os.path.join(t, "**", "*.pfw*"), recursive=True))
    return sorted(glob.glob(t, recursive=True))


class Tail:
    """Incremental reader of every trace file under ``traces``; new files are picked up on each poll."""

    def __init__(self, traces: str) -> None:
        self.traces = traces
        self.pos: Dict[str, int] = {}
        self.stalled = 0                     # polls that found bytes but no complete member yet

    def poll(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for f in expand(self.traces):
            out.extend(self._read(f))
        return out

    def _read(self, path: str) -> List[Dict[str, Any]]:
        at = self.pos.get(path, 0)
        try:
            if os.path.getsize(path) <= at:
                return []
            with open(path, "rb") as fh:
                fh.seek(at)
                data = fh.read(READ_LIMIT)
        except OSError:
            return []
        if not path.endswith(".gz"):
            end = data.rfind(b"\n") + 1
            self.pos[path] = at + end
            return _lines(data[:end])
        recs, used = [], 0
        while used < len(data):
            d = zlib.decompressobj(zlib.MAX_WBITS | 16)
            try:
                text = d.decompress(data[used:])
            except zlib.error:
                self.stalled += 1            # a hole: the member before this one is not written yet
                break
            if not d.eof:
                self.stalled += 1            # the member is still being written
                break
            used = len(data) - len(d.unused_data)
            recs.extend(_lines(text))
        self.pos[path] = at + used
        return recs


def _lines(raw: bytes) -> List[Dict[str, Any]]:
    out = []
    for line in raw.splitlines():
        line = line.strip().rstrip(b",")
        if not line.startswith(b"{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class Graph:
    """The provenance graph of a set of records, built incrementally."""

    def __init__(self) -> None:
        self.types: Dict[str, Dict[str, Any]] = {}
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.activities: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, str]] = []            # activity relations: {activity, kind, entity}
        self.relations: List[Dict[str, str]] = []        # entity relations: {relation, subject, object}
        self.events = Counter()                          # every other event, by category
        self.total = 0
        self._seen = set()

    def add(self, r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Take one record; returns what it added to the graph (what a live view shows), or None."""
        self.total += 1
        name, cat, a = r.get("name"), r.get("cat"), r.get("args") or {}
        if name == "ET" and a.get("type"):
            if a["type"] in self.types:
                return None
            role = a.get("role", 0)
            self.types[a["type"]] = {"type": a["type"], "role": ROLES[role] if isinstance(role, int) and role < len(ROLES) else str(role),
                                     "description": a.get("description", "")}
            return {"kind": "type", **self.types[a["type"]]}
        if name == "EH" and a.get("id"):
            if a["id"] in self.entities:
                return None
            store = a.get("store", 8)
            self.entities[a["id"]] = {"id": a["id"], "type": a.get("type", ""), "uri": a.get("uri", ""),
                                      "store": STORES[store] if isinstance(store, int) and store < len(STORES) else str(store)}
            return {"kind": "entity", **self.entities[a["id"]]}
        if name == "ER" and a.get("subject"):
            key = (a.get("relation"), a["subject"], a.get("object"))
            if key in self._seen:
                return None
            self._seen.add(key)
            rec = {"relation": ENTITY_RELATIONS.get(a.get("relation"), str(a.get("relation"))), "subject": a["subject"], "object": a.get("object", "")}
            self.relations.append(rec)
            return {"kind": "relation", **rec}
        if cat in ACTIVITY_CATS and r.get("ph") in (1, "X"):
            aid = f"{r.get('pid')}:{r.get('ts')}"
            if aid in self._seen:
                return None
            self._seen.add(aid)
            act = {"id": aid, "name": name, "pid": r.get("pid"), "ts": r.get("ts"), "dur": r.get("dur", 0)}
            rels = a.get("relations") or {}
            for kind, ids in rels.items():
                for e in ids if isinstance(ids, list) else [ids]:
                    self.edges.append({"activity": aid, "kind": kind, "entity": e})
            self.activities.append(act)
            return {"kind": "activity", **act, "relations": rels}
        if r.get("ph") != 4 and cat:
            self.events[cat] += 1
        return None

    def extend(self, recs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [d for d in (self.add(r) for r in recs) if d]

    def dangling(self) -> List[str]:
        """Entity ids an activity or relation names, with no EH record."""
        ids = {e["entity"] for e in self.edges} | {x for r in self.relations for x in (r["subject"], r["object"])}
        return sorted(i for i in ids if i and i not in self.entities)

    def counts(self) -> Dict[str, Any]:
        return {"activities": len(self.activities), "entities": len(self.entities), "edges": len(self.edges),
                "relations": len(self.relations), "types": len(self.types), "records": self.total,
                "other_events": dict(self.events), "dangling": len(self.dangling())}

    def summary(self) -> Dict[str, Any]:
        by_type = Counter(e["type"] for e in self.entities.values())
        by_store = Counter(e["store"] for e in self.entities.values())
        by_act = {}
        for a in self.activities:
            d = by_act.setdefault(a["name"], {"name": a["name"], "count": 0, "total_us": 0})
            d["count"] += 1
            d["total_us"] += a["dur"] or 0
        return {**self.counts(), "by_type": dict(by_type), "by_store": dict(by_store), "types": list(self.types.values()),
                "activity_kinds": sorted(by_act.values(), key=lambda x: -x["total_us"]), "edge_kinds": dict(Counter(e["kind"] for e in self.edges)),
                "relation_kinds": dict(Counter(r["relation"] for r in self.relations))}

    def lineage(self, entity: str, direction: str = "back", limit: int = 400) -> Dict[str, Any]:
        """Walk from an entity (id, or uri/type substring) backward to its inputs or forward to what it fed."""
        start = self.find(entity)
        if not start:
            return {"status": "error", "error": f"no entity matches {entity!r}"}
        produce, consume = ("generated", "used") if direction == "back" else ("used", "generated")
        by_ent: Dict[Tuple[str, str], List[str]] = {}
        by_act: Dict[Tuple[str, str], List[str]] = {}
        for e in self.edges:
            by_ent.setdefault((e["entity"], e["kind"]), []).append(e["activity"])
            by_act.setdefault((e["activity"], e["kind"]), []).append(e["entity"])
        names = {a["id"]: a["name"] for a in self.activities}
        seen_e, seen_a, steps, frontier, hops = set(start), set(), [], list(start), 0
        while frontier and len(seen_e) < limit:
            hops += 1
            nxt = []
            for ent in frontier:
                for act in by_ent.get((ent, produce), []):
                    if act in seen_a:
                        continue
                    seen_a.add(act)
                    new = [x for x in by_act.get((act, consume), []) if x not in seen_e]
                    seen_e.update(new)
                    nxt.extend(new)
                    steps.append({"hop": hops, "activity": names.get(act, act), "from": ent, "to": new})
            frontier = nxt
        roots = [self.entities.get(i, {"id": i}) for i in sorted(seen_e) if i not in start and (i, produce) not in by_ent]
        return {"status": "ok", "direction": direction, "start": start, "hops": hops, "steps": steps[:limit], "reached": len(seen_e),
                "ends": roots[:limit]}

    def find(self, q: str) -> List[str]:
        if q in self.entities:
            return [q]
        return [i for i, e in self.entities.items() if q and (q in e.get("uri", "") or q == e.get("type"))]

    def view(self, limit: int = 1500) -> Dict[str, Any]:
        """Everything the Dashboard needs for its first paint (later changes arrive as pushed deltas)."""
        return {"types": list(self.types.values()), "entities": list(self.entities.values())[-limit:], "activities": self.activities[-limit:],
                "edges": self.edges[-limit * 2:], "relations": self.relations[-limit:], "counts": self.counts()}


def read(traces: str) -> Graph:
    """The whole graph of finished traces."""
    g, t = Graph(), Tail(traces)
    while True:
        recs = t.poll()
        if not recs:
            return g
        g.extend(recs)
