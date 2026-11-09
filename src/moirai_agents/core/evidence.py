"""Evidence-backed answers: a question is resolved into concrete entities and time frames, the actual provenance records are
queried with dftracer-utils' TraceViewer, the result is computed, and only then is it written up for the user - with every
claim citing the queries and records it rests on.

    plan       a short list of steps (the only part the agent chooses; the page builds them itself for one-click questions):
                 entities    {type?, uri_contains?, id?, ids?, match? (id, type or uri part), all?, produced_since?, produced_until?}
                 activities  {name?, pid?, since?, until?}
                 lineage     {from: step, direction: back|forward, max_hops?}
                 group       {from: step, by: type|role|store|name|pid}
                 ends        {from: step}                         roots of a backward lineage, leaves of a forward one
                 inputs      {activity, since?, until?}           the parameters each run of an activity used (arguments, object attributes,
                                                                  command-line flags, named values: its "<activity>.inputs" entities)
                 outputs     {activity, since?, until?}           what each run returned ("<activity>.output")
                 attributes  {from: step}                         the recorded attributes of a step's entities ("<type>.attributes", part_of)
               times: ISO date-time, epoch seconds or microseconds, or relative to the run: "start+30m", "end-2h".
               "from" is the 0-based index of an earlier step.
    resolve    each step's selectors are made concrete before any query: entity type names checked against the types the
               traces declare, time frames turned into trace timestamps (microseconds), shown in the trail.
    query      every selection is a TraceViewer query on every trace file - entity records by type/id/uri, activities by name,
               pid and time_range, and each lineage hop as the activities whose used/generated relations hold the frontier
               entities - and the trail keeps each query's text, file fingerprints, rows and time.
    result     computed from the query results: a headline and tables, by template, with a hash of every step's output.
    synthesis  the agent's write-up for the user (optional): it must cite steps as [S1]..[Sn]; a citation to no step, or a
               number that is not in the computed result, is flagged next to it. The computed result is always shown.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from . import pipeline as P
from . import provtrace as T

MAX_STEPS, MAX_HOPS, MAX_NODES, MAX_CITE, MAX_ROWS, MAX_IN = 8, 30, 5000, 300, 200, 4000
OPS = ("entities", "activities", "lineage", "group", "ends", "inputs", "outputs", "attributes")
ACT = 'col("cat").is_in(["moirai", "PROV"])'


class PlanError(ValueError):
    pass


# ------------------------------------------------------------------ TraceViewer, with every query kept
def _fingerprint(f: str) -> Dict[str, Any]:
    st = os.stat(f)
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        h.update(fh.read(1 << 20))
        if st.st_size > 2 << 20:
            fh.seek(-(1 << 20), os.SEEK_END)
            h.update(fh.read(1 << 20))
    return {"file": os.path.basename(f), "path": f, "bytes": st.st_size, "mtime": round(st.st_mtime, 3), "sha256_ends": h.hexdigest()[:16]}


def _clean(r: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in r.items() if v is not None and v != ""}


class Viewer:
    """TraceViewer over each trace file; ``q`` runs one query on all of them and keeps its text, rows and time."""
    def __init__(self, traces: str, index_dir: str) -> None:
        import dftracer.utils as U
        try:
            U.set_log_level("warning")
        except Exception:  # noqa: BLE001
            pass
        self.U, self.files, self.idx = U, T.expand(traces), index_dir
        if not self.files:
            raise PlanError(f"no trace file under {traces}")
        self.queries: List[Dict[str, Any]] = []
        self.cols: Dict[str, List[str]] = {}

    def columns(self, f: str) -> List[str]:
        if f not in self.cols:
            self.cols[f] = list(self.U.TraceViewer(f, self.idx).columns)
        return self.cols[f]

    def q(self, why: str, text: str, build: Callable[[Any, Any, List[str]], Any]) -> List[Dict[str, Any]]:
        """build(tv, col, columns) -> a TraceViewer query (or None: nothing to ask in that file); text: how it reads, for the trail."""
        rows, t0, per = [], time.time(), {}
        for f in self.files:
            tv = self.U.TraceViewer(f, self.idx).phase("any")
            q = build(tv, self.U.col, self.columns(f))
            got = [] if q is None else [_clean(r) for r in q.collect().to_dicts()]
            per[os.path.basename(f)] = len(got)
            rows += [{**r, "_file": os.path.basename(f)} for r in got]
        self.queries.append({"n": len(self.queries) + 1, "why": why, "query": f'TraceViewer(<file>).phase("any"){text}', "rows": len(rows),
                             "per_file": per, "secs": round(time.time() - t0, 3)})
        return rows

    def reads(self) -> List[Dict[str, Any]]:
        return [_fingerprint(f) for f in self.files]


def _or_in(col, cols: List[str], prefix: str, ids: List[str]):
    """any of the relation columns <prefix>.0, .1, ... holds one of ids (None when the file has no such column)."""
    pred = None
    for c in cols:
        if c.startswith(prefix + ".") or c == prefix:
            p = col(c).is_in(ids)
            pred = p if pred is None else (pred | p)
    return pred


def _rel(row: Dict[str, Any], kind: str) -> List[str]:
    out = []
    for k, v in row.items():
        if k.startswith(f"args.relations.{kind}.") or k == f"args.relations.{kind}":
            out += v if isinstance(v, list) else [v]
    return [str(x) for x in out if x not in (None, "")]


def _ref(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"file": r.get("_file"), "rec": r.get("name"), "pid": r.get("pid"), "ts": r.get("ts"), **({"id": str(r["args.id"])} if r.get("args.id") else {})}


def _act(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f"{r.get('pid')}:{r.get('ts')}", "name": r.get("name"), "pid": r.get("pid"), "ts": r.get("ts"), "dur": r.get("dur", 0),
            "used": _rel(r, "used"), "generated": _rel(r, "generated") + _rel(r, "updated"), "ref": _ref(r)}


# ------------------------------------------------------------------ the catalog: entity types and entities (metadata records)
def _catalog(v: Viewer) -> Dict[str, Any]:
    rows = v.q("catalog: the entity types and entities the traces declare", '.filter(col("name").is_in(["ET", "EH"]))',
               lambda tv, col, cols: tv.filter(col("name").is_in(["ET", "EH"])))
    types, ents = {}, {}
    for r in rows:
        if r.get("name") == "ET" and r.get("args.type"):
            role = r.get("args.role", 0)
            types.setdefault(r["args.type"], {"type": r["args.type"], "role": T.ROLES[role] if isinstance(role, int) and 0 <= role < len(T.ROLES) else str(role),
                                              "description": r.get("args.description", "")})
        elif r.get("name") == "EH" and r.get("args.id"):
            st = r.get("args.store", 8)
            ents.setdefault(str(r["args.id"]), {"id": str(r["args.id"]), "type": r.get("args.type", ""), "uri": r.get("args.uri", ""),
                                               "store": T.STORES[st] if isinstance(st, int) and 0 <= st < len(T.STORES) else str(st), "ref": _ref(r)})
    return {"types": types, "entities": ents}


# ------------------------------------------------------------------ resolving times
_REL = re.compile(r"^(start|end)\s*([+-])\s*(\d+(?:\.\d+)?)\s*(us|ms|s|m|h|d)?$")
_UNIT = {"us": 1, "ms": 1e3, "s": 1e6, "m": 60e6, "h": 3600e6, "d": 86400e6, None: 1e6}


def _span(v: Viewer, ctx: Dict[str, Any]) -> List[int]:
    if "span" not in ctx:
        rows = v.q("the run's time span (for relative times)", f".filter({ACT}).select(['ts', 'dur'])",
                   lambda tv, col, cols: tv.filter(col("cat").is_in(["moirai", "PROV"])).select(["ts", "dur"]))
        ts = [r for r in rows if isinstance(r.get("ts"), (int, float))]
        ctx["span"] = [min(r["ts"] for r in ts), max(r["ts"] + (r.get("dur") or 0) for r in ts)] if ts else [0, 0]
    return ctx["span"]


def resolve_time(x: Any, v: Viewer, ctx: Dict[str, Any]) -> Optional[int]:
    """A time as trace microseconds: ISO, epoch seconds or microseconds, or start+30m / end-2h."""
    if x in (None, ""):
        return None
    if isinstance(x, (int, float)):
        return int(x if x > 1e14 else x * 1e6)
    s = str(x).strip()
    m = _REL.match(s)
    if m:
        a, b = _span(v, ctx)
        d = float(m.group(3)) * _UNIT[m.group(4)]
        return int((a if m.group(1) == "start" else b) + (d if m.group(2) == "+" else -d))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return resolve_time(float(s), v, ctx)
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1e6)
    except ValueError:
        raise PlanError(f"time {s!r}: give ISO (2026-11-08T14:00), epoch seconds, or start+30m / end-2h")


def _iso(us: Optional[int]) -> str:
    return datetime.fromtimestamp(us / 1e6).isoformat(timespec="seconds") if us else "-"


# ------------------------------------------------------------------ steps: each a TraceViewer query (or computed from earlier steps)
def _from(s, k, outs, kinds):
    f = s.get("from")
    if not isinstance(f, int) or not 0 <= f < k:
        raise PlanError(f"step {k + 1} ({s['op']}): 'from' must be an earlier step's index, 0..{k - 1}")
    if outs[f]["kind"] not in kinds:
        raise PlanError(f"step {k + 1} ({s['op']}): step index {f} gives {outs[f]['kind']}, it needs {' or '.join(kinds)}")
    return outs[f]


def _entities(s, k, v, cat, outs, ctx):
    t, u, i, ids, every, mt = s.get("type"), s.get("uri_contains"), s.get("id"), s.get("ids"), bool(s.get("all")), s.get("match")
    if t and t not in cat["types"] and not any(e["type"] == t for e in cat["entities"].values()):
        raise PlanError(f"step {k + 1}: entity type {t!r} is not in these traces; they declare {sorted(cat['types'])}")
    if not (t or u or i or ids or every or mt):
        raise PlanError(f"step {k + 1} (entities): give type, uri_contains, id, ids or match (or all: true)")
    want = ([str(i)] if i else []) + [str(x) for x in (ids or [])]
    resolved = {"type": t, "uri_contains": u, "ids": want, **({"match": mt} if mt else {})}
    text = ['col("name") == "EH"'] + ([f'col("args.type") == {t!r}'] if t else []) + ([f'col("args.uri").contains({u!r})'] if u else []) + \
           ([f'col("args.id").is_in({want[:5]}{"…" if len(want) > 5 else ""})'] if want else []) + \
           ([f'(col("args.id") == {mt!r} | col("args.type") == {mt!r} | col("args.uri").contains({mt!r}))'] if mt else [])

    def build(tv, col, cols):
        p = col("name") == "EH"
        if t:
            p = p & (col("args.type") == t)
        if u:
            p = p & col("args.uri").contains(u)
        if want:
            p = p & col("args.id").is_in(want)
        if mt:
            p = p & ((col("args.id") == mt) | (col("args.type") == mt) | col("args.uri").contains(mt))
        return tv.filter(p)
    rows = v.q(f"step {k + 1}: the entities", ".filter(" + " & ".join(text) + ")", build)
    got = sorted({str(r["args.id"]) for r in rows if r.get("args.id")})
    a0, a1 = resolve_time(s.get("produced_since"), v, ctx), resolve_time(s.get("produced_until"), v, ctx)
    if a0 is not None or a1 is not None:           # entity records carry no time: an entity is in the frame when its producer is
        lo, hi = a0 if a0 is not None else 0, a1 if a1 is not None else 2 ** 62
        resolved["produced"] = [_iso(a0), _iso(a1)]

        def bprod(tv, col, cols):
            p = _or_in(col, cols, "args.relations.generated", got)
            return None if p is None else tv.filter(col("cat").is_in(["moirai", "PROV"]) & p).time_range(lo, hi)
        prod = v.q(f"step {k + 1}: of those, the ones produced in the time frame",
                   f'.filter({ACT} & any(args.relations.generated.*).is_in(<{len(got)} ids>)).time_range({lo}, {hi})', bprod) if got else []
        inside = {x for r in prod for x in _rel(r, "generated")}
        got = [g for g in got if g in inside]
        rows = [r for r in rows if str(r.get("args.id")) in inside] + prod
    return {"kind": "entities", "ids": got, "resolved": resolved, "cites": [_ref(r) for r in rows][:MAX_CITE]}


def _activities(s, k, v, cat, outs, ctx):
    nm, pid = s.get("name"), s.get("pid")
    a0, a1 = resolve_time(s.get("since"), v, ctx), resolve_time(s.get("until"), v, ctx)
    timed = a0 is not None or a1 is not None
    lo, hi = a0 if a0 is not None else 0, a1 if a1 is not None else 2 ** 62
    text = [ACT] + ([f'col("name") == {nm!r}'] if nm else []) + ([f'col("pid") == {pid}'] if pid is not None else [])

    def build(tv, col, cols):
        p = col("cat").is_in(["moirai", "PROV"])
        if nm:
            p = p & (col("name") == nm)
        if pid is not None:
            p = p & (col("pid") == pid)
        q = tv.filter(p)
        return q.time_range(lo, hi) if timed else q
    rows = v.q(f"step {k + 1}: the activities", ".filter(" + " & ".join(text) + ")" + (f".time_range({lo}, {hi})" if timed else ""), build)
    acts = sorted({a["id"]: a for a in map(_act, rows)}.values(), key=lambda a: (a["ts"] or 0, a["pid"] or 0))
    ctx.setdefault("acts", {}).update({a["id"]: a for a in acts})
    return {"kind": "activities", "ids": [a["id"] for a in acts], "resolved": {"name": nm, "pid": pid, "time": [_iso(a0), _iso(a1)] if timed else None},
            "cites": [a["ref"] for a in acts][:MAX_CITE]}


def _lineage(s, k, v, cat, outs, ctx):
    src = _from(s, k, outs, ("entities", "lineage"))
    start = src["ids"] if src["kind"] == "entities" else src["start"]
    if not start:
        raise PlanError(f"step {k + 1} (lineage): the step it starts from found no entity")
    d = s.get("direction", "back")
    if d not in ("back", "forward"):
        raise PlanError(f"step {k + 1} (lineage): direction is back or forward")
    produce, consume = ("generated", "used") if d == "back" else ("used", "generated")
    maxh = min(int(s.get("max_hops") or MAX_HOPS), MAX_HOPS)
    seen_e, seen_a, hops, frontier, h, cites = set(start), {}, [], list(start), 0, []
    while frontier and h < maxh and len(seen_e) + len(seen_a) < MAX_NODES:
        h += 1
        ids = frontier[:MAX_IN]

        def build(tv, col, cols, ids=ids):
            p = _or_in(col, cols, f"args.relations.{produce}", ids)
            return None if p is None else tv.filter(col("cat").is_in(["moirai", "PROV"]) & p)
        rows = v.q(f"step {k + 1}, hop {h}: activities that {'generated' if d == 'back' else 'used'} the {len(ids)} entities reached so far",
                   f'.filter({ACT} & any(args.relations.{produce}.*).is_in(<{len(ids)} ids>))', build)
        nxt, fs = [], set(ids)
        for a in map(_act, rows):
            if a["id"] in seen_a:
                continue
            seen_a[a["id"]] = a
            cites.append(a["ref"])
            new = [x for x in a[consume] if x not in seen_e]
            seen_e.update(new)
            nxt += new
            hops.append({"hop": h, "activity": a["id"], "name": a["name"], "from": [x for x in a[produce] if x in fs], "to": a[consume], "ref": a["ref"]})
        frontier = nxt
    ctx.setdefault("acts", {}).update(seen_a)
    has_producer = {x for a in seen_a.values() for x in a[produce]}
    ends = sorted(i for i in seen_e if i not in start and i not in has_producer)
    cites = [cat["entities"][i]["ref"] for i in sorted(seen_e) if i in cat["entities"]] + cites
    return {"kind": "lineage", "direction": d, "start": sorted(start), "entities": sorted(seen_e), "activities": sorted(seen_a), "hops": hops, "ends": ends,
            "dangling": sorted(i for i in seen_e if i not in cat["entities"]), "cites": cites[:MAX_CITE], "truncated": bool(frontier)}


def _group(s, k, v, cat, outs, ctx):
    src = _from(s, k, outs, ("entities", "activities", "lineage"))
    by = s.get("by", "type")
    acts = ctx.get("acts", {})
    if src["kind"] == "activities" or (src["kind"] == "lineage" and by in ("name", "pid")):
        ids = src["ids"] if src["kind"] == "activities" else src["activities"]
        c = Counter(str(acts[i].get("pid" if by == "pid" else "name")) for i in ids if i in acts)
    else:
        ids = src["ids"] if src["kind"] == "entities" else [i for i in src["entities"] if i not in src["start"]]
        if by == "role":
            c = Counter((cat["types"].get(cat["entities"][i]["type"]) or {}).get("role", "?") for i in ids if i in cat["entities"])
        else:
            c = Counter(str(cat["entities"][i].get(by, "?")) for i in ids if i in cat["entities"])
    return {"kind": "group", "by": by, "of": s["from"], "counts": dict(c.most_common()), "cites": src["cites"][:MAX_CITE]}


def _ends(s, k, v, cat, outs, ctx):
    src = _from(s, k, outs, ("lineage",))
    return {"kind": "entities", "ids": src["ends"], "of": "roots" if src["direction"] == "back" else "leaves",
            "cites": [cat["entities"][i]["ref"] for i in src["ends"] if i in cat["entities"]][:MAX_CITE]}


# ---- captured inputs, outputs and attributes (entities whose uri is "json:{...}")
def value_of(uri: str) -> Any:
    if isinstance(uri, str) and uri.startswith("json:"):
        try:
            return json.loads(uri[5:])
        except ValueError:
            return {"unreadable": uri[:200]}
    return None


def _flat(v: Any, path: str, out: Dict[str, Any], depth: int = 0) -> None:
    """Parameters as name -> value: flags first, then named values, then scalar arguments and object attributes."""
    if isinstance(v, dict) and depth < 4:
        for k, x in v.items():
            _flat(x, f"{path}.{k}" if path else str(k), out, depth + 1)
    elif isinstance(v, list) and len(v) <= 8 and all(not isinstance(x, (dict, list)) for x in v):
        out[path] = ",".join(str(x) for x in v)
    elif not isinstance(v, (dict, list)):
        out[path] = v


def params_of(value: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for src, v in (value or {}).get("flags", {}).items():
        for k, x in v.items():
            out[f"--{k}" if len(k) > 1 else f"-{k}"] = x
    _flat((value or {}).get("values", {}), "", out)
    args = (value or {}).get("args", {})
    flat: Dict[str, Any] = {}
    _flat(args, "", flat)
    for k, x in flat.items():
        k2 = k.replace(".attrs.", ".")
        if isinstance(x, str) and len(x) > 200:
            continue                                  # a long string (a command line) is shown by its flags
        if not k2.endswith(".type") and k2 not in out:
            out[k2] = x
    return out


def _captured(s, k, v, cat, outs, ctx, suffix, relation):
    name = s.get("activity")
    if not name:
        raise PlanError(f"step {k + 1} ({s['op']}): give activity, the activity's name")
    sub = _activities({"op": "activities", "name": name, "since": s.get("since"), "until": s.get("until")}, k, v, cat, outs, ctx)
    acts = [ctx["acts"][i] for i in sub["ids"]]
    if not acts:
        raise PlanError(f"step {k + 1}: no activity named {name!r} in these traces")
    ids = sorted({x for a in acts for x in a[relation]})
    et = f"{name}.{suffix}"

    def build(tv, col, cols):
        return tv.filter((col("name") == "EH") & (col("args.type") == et) & col("args.id").is_in(ids)) if ids else None
    rows = v.q(f"step {k + 1}: the {suffix} entities of those {len(acts)} {name} runs",
               f'.filter(col("name") == "EH" & col("args.type") == {et!r} & col("args.id").is_in(<{len(ids)} ids>))', build)
    vals = {str(r["args.id"]): value_of(r.get("args.uri", "")) for r in rows if r.get("args.id")}
    runs = []
    for a in acts:
        got = [vals[x] for x in a[relation] if x in vals]
        runs.append({"activity": a["id"], "pid": a["pid"], "ts": a["ts"], "value": got[0] if got else None, "ref": a["ref"]})
    return {"kind": suffix, "activity": name, "runs": runs, "ids": sorted(vals),
            "resolved": {"activity": name, "time": sub["resolved"]["time"]}, "cites": ([a["ref"] for a in acts] + [_ref(r) for r in rows])[:MAX_CITE]}


def _inputs(s, k, v, cat, outs, ctx):
    return _captured(s, k, v, cat, outs, ctx, "inputs", "used")


def _outputs(s, k, v, cat, outs, ctx):
    return _captured(s, k, v, cat, outs, ctx, "output", "generated")


def _attributes(s, k, v, cat, outs, ctx):
    src = _from(s, k, outs, ("entities", "lineage"))
    ids = src["ids"] if src["kind"] == "entities" else src["entities"]
    rel = v.q(f"step {k + 1}: the attributes entities part_of those {len(ids)} entities", '.filter(col("name") == "ER" & col("args.object").is_in(<ids>))',
              lambda tv, col, cols: tv.filter((col("name") == "ER") & col("args.object").is_in(ids)) if ids else None)
    subj = sorted({str(r["args.subject"]) for r in rel if r.get("args.subject")})
    rows = v.q(f"step {k + 1}: those attributes entities", '.filter(col("name") == "EH" & col("args.id").is_in(<ids>))',
               lambda tv, col, cols: tv.filter((col("name") == "EH") & col("args.id").is_in(subj)) if subj else None)
    of = {str(r["args.subject"]): str(r.get("args.object")) for r in rel}
    runs = [{"entity": of.get(str(r["args.id"])), "value": value_of(r.get("args.uri", "")), "ref": _ref(r)} for r in rows if str(r.get("args.type", "")).endswith(".attributes")]
    return {"kind": "attributes", "runs": runs, "ids": [x["entity"] for x in runs], "cites": ([_ref(r) for r in rel] + [x["ref"] for x in runs])[:MAX_CITE]}


STEPS = {"entities": _entities, "activities": _activities, "lineage": _lineage, "group": _group, "ends": _ends,
         "inputs": _inputs, "outputs": _outputs, "attributes": _attributes}


def check_plan(steps: Any) -> List[str]:
    if not isinstance(steps, list) or not steps:
        return ["a plan is a non-empty list of steps"]
    if len(steps) > MAX_STEPS:
        return [f"at most {MAX_STEPS} steps"]
    return [f"step {k + 1}: op must be one of {OPS}" for k, s in enumerate(steps) if not isinstance(s, dict) or s.get("op") not in OPS]


# ------------------------------------------------------------------ the computed result, by template
def _shown(uri: str) -> str:
    v = value_of(uri)
    if v is None:
        return uri or "-"
    p = params_of(v) if isinstance(v, dict) and ("args" in v or "flags" in v or "values" in v) else None
    return ", ".join(f"{k}={x}" for k, x in list(p.items())[:6]) if p else json.dumps(v, default=str)[:160]


def _label(cat, i):
    e = cat["entities"].get(i)
    if e and (e.get("uri") or "").startswith("json:"):
        return f"{e['type']} ({_shown(e['uri'])[:60]})"
    return f"{e['type']} {os.path.basename((e['uri'] or i).rstrip('/')) or i}" if e else f"undeclared {i}"


def _types(cat, ids):
    c = Counter(cat["entities"][i]["type"] for i in ids if i in cat["entities"])
    return ", ".join(f"{n} {t}" for t, n in c.most_common()) or "none"


def _table(head, rows):
    if not rows:
        return ""
    e = lambda x: str(x).replace("|", "\\|").replace("\n", " ")   # noqa: E731
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(e(x) for x in r) + " |" for r in rows[:MAX_ROWS]])


def headline(o, cat, ctx) -> str:
    k = o["kind"]
    if k == "entities":
        what = {"roots": "roots (inputs nothing in the traces produced)", "leaves": "final entities (nothing in the traces used them)"}.get(o.get("of"), "entities")
        return f"{len(o['ids'])} {what}: {_types(cat, o['ids'])}."
    if k == "activities":
        acts = [ctx["acts"][i] for i in o["ids"] if i in ctx.get("acts", {})]
        c = Counter(a["name"] for a in acts)
        return f"{len(acts)} activities ({', '.join(f'{n} {t}' for t, n in c.most_common()) or 'none'})" + (f", {_iso(acts[0]['ts'])} to {_iso(acts[-1]['ts'])}." if acts else ".")
    if k == "lineage":
        start = ", ".join(_label(cat, i) for i in o["start"][:3]) + (f" and {len(o['start']) - 3} more" if len(o["start"]) > 3 else "")
        others = [i for i in o["entities"] if i not in o["start"]]
        nh = max([x["hop"] for x in o["hops"]], default=0)
        if o["direction"] == "back":
            s = (f"{start} came from {len(o['ends'])} root input(s) ({_types(cat, o['ends'])}) through {len(o['activities'])} activities over {nh} hop(s); "
                 f"{len(others)} upstream entities in all ({_types(cat, others)}).")
        else:
            s = f"{start} affected {len(others)} downstream entities ({_types(cat, others)}) through {len(o['activities'])} activities over {nh} hop(s); {len(o['ends'])} of them are final."
        if o["dangling"]:
            s += f" {len(o['dangling'])} referenced id(s) have no entity record, so that lineage stops short."
        if o["truncated"]:
            s += " The walk stopped at its limit: there is more."
        return s
    if k == "group":
        return f"By {o['by']}: " + ", ".join(f"{g} {n}" for g, n in o["counts"].items()) + "."
    if k in ("inputs", "output", "attributes"):
        merged = _merged(o)
        what = {"inputs": f"{o.get('activity')} ran {len(o['runs'])} time(s); its inputs", "output": f"{o.get('activity')} ran {len(o['runs'])} time(s); what it returned",
                "attributes": f"{len(o['runs'])} attribute record(s)"}[k]
        if not merged:
            return what + ": none recorded (the annotation does not capture them: annotate again with capture on)."
        shown = "; ".join(f"{p} = {' | '.join(map(str, vs[:4]))}{' …' if len(vs) > 4 else ''}" for p, (vs, n) in list(merged.items())[:12])
        return f"{what}: {shown}" + (f"; and {len(merged) - 12} more" if len(merged) > 12 else "") + "."
    return ""


def _merged(o) -> Dict[str, Any]:
    """parameter -> (distinct values, runs that had it), over every run of the step."""
    seen: Dict[str, Any] = {}
    for r in o["runs"]:
        val = r.get("value")
        flat = params_of(val) if o["kind"] == "inputs" else ({} if val is None else {})
        if o["kind"] != "inputs" and val is not None:
            _flat(val, "", flat)
        for p, x in flat.items():
            vs, n = seen.setdefault(p, ([], 0))
            if x not in vs:
                vs.append(x)
            seen[p] = (vs, n + 1)
    return seen


def table(o, cat, ctx) -> str:
    k = o["kind"]
    if k == "entities":
        return _table(["type", "entity", "store", "id"], [[cat["entities"][i]["type"], _shown(cat["entities"][i]["uri"]), cat["entities"][i]["store"], i] for i in o["ids"] if i in cat["entities"]])
    if k == "activities":
        return _table(["activity", "pid", "start", "ts (µs)", "used", "generated"], [[a["name"], a["pid"], _iso(a["ts"]), a["ts"], len(a["used"]), len(a["generated"])]
                                                                                      for a in (ctx["acts"][i] for i in o["ids"] if i in ctx.get("acts", {}))])
    if k == "lineage":
        return _table(["hop", "activity", "pid", "produced" if o["direction"] == "back" else "input", "used" if o["direction"] == "back" else "produced"],
                      [[x["hop"], x["name"], x["ref"]["pid"], ", ".join(_label(cat, t) for t in x["from"][:3]), ", ".join(_label(cat, t) for t in x["to"][:4]) + (" …" if len(x["to"]) > 4 else "")]
                       for x in o["hops"]])
    if k == "group":
        return _table([o["by"], "count"], [[g, n] for g, n in o["counts"].items()])
    if k in ("inputs", "output", "attributes"):
        return _table(["name", "value(s)", "runs"], [[p, " | ".join(map(str, vs[:8])) + (" …" if len(vs) > 8 else ""), n] for p, (vs, n) in _merged(o).items()])
    return ""


def _hash(outs) -> str:
    return hashlib.sha256(json.dumps([{k: v for k, v in o.items() if k != "cites"} for o in outs], sort_keys=True, default=str).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ the synthesis check
_CIT = re.compile(r"\[S(\d+)\]")


def check_synthesis(text: str, n_steps: int, computed: str) -> Dict[str, Any]:
    """Every [Sk] names a step; every number in the text appears in the computed result (headlines, tables, counts)."""
    if not text:
        return {"ok": True, "problems": [], "citations": []}
    cited = sorted({int(m) for m in _CIT.findall(text)})
    problems = [f"[S{c}] is not a step (1..{n_steps})" for c in cited if not 1 <= c <= n_steps]
    if not cited:
        problems.append("no citation: every claim cites the step it comes from, as [S1]..[Sn]")
    known = set(re.findall(r"\d+(?:\.\d+)?", computed))
    for num in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", _CIT.sub("", text)):
        if num not in known:
            problems.append(f"the number {num} is not in the computed result")
    return {"ok": not problems, "problems": problems[:12], "citations": cited}


# ------------------------------------------------------------------ the whole thing
def run(traces: str, steps: List[Dict[str, Any]], index_dir: str) -> Dict[str, Any]:
    bad = check_plan(steps)
    if bad:
        raise PlanError("; ".join(bad))
    t0 = time.time()
    v = Viewer(traces, index_dir)
    cat = _catalog(v)
    ctx: Dict[str, Any] = {}
    outs, res = [], []
    for k, s in enumerate(steps):
        before = len(v.queries)
        o = STEPS[s["op"]](s, k, v, cat, outs, ctx)
        outs.append(o)
        res.append({"step": k + 1, "op": s["op"], "args": {a: x for a, x in s.items() if a != "op"}, "resolved": o.get("resolved"),
                    "queries": [q["n"] for q in v.queries[before:]] or None, "computed_from": None if v.queries[before:] else s.get("from"),
                    "headline": headline(o, cat, ctx), "table": table(o, cat, ctx), "cites": o["cites"], "cited": len(o["cites"])})
    lin = next((o for o in reversed(outs) if o["kind"] == "lineage"), None)
    return {"steps": res, "queries": v.queries, "reads": v.reads(), "outs": outs, "result_hash": _hash(outs), "secs": round(time.time() - t0, 3),
            "engine": {"name": "dftracer-utils TraceViewer", "version": getattr(v.U, "__version__", "?")},
            "computed": "\n".join(r["headline"] + "\n" + r["table"] for r in res),
            "graph": _graph(lin, cat, ctx) if lin else None, "types": cat["types"],
            "stats": {"sources": len(v.files), "queries": len(v.queries), "rows_read": sum(q["rows"] for q in v.queries),
                      "entities_declared": len(cat["entities"]), "cited": sum(len(o["cites"]) for o in outs)}}


def _graph(o, cat, ctx, cap: int = 150) -> Dict[str, Any]:
    acts = ctx.get("acts", {})
    ents, aids = o["entities"][:cap], o["activities"][:cap]
    keep = set(ents) | set(aids)
    nodes = [{"id": i, "kind": "entity", "type": (cat["entities"].get(i) or {}).get("type", ""), "uri": (cat["entities"].get(i) or {}).get("uri", "")[:400],
              "label": (_label(cat, i) if ((cat["entities"].get(i) or {}).get("uri") or "").startswith("json:") else os.path.basename(((cat["entities"].get(i) or {}).get("uri") or i).rstrip("/")) or i)[:40],
              "start": i in o["start"],
              "end": i in o["ends"]} for i in ents]
    nodes += [{"id": a, "kind": "activity", "label": acts[a]["name"], "pid": acts[a]["pid"], "ts": acts[a]["ts"], "dur": acts[a]["dur"]} for a in aids if a in acts]
    edges = []
    for a in aids:
        if a in acts:
            edges += [{"source": e, "target": a, "kind": "used"} for e in acts[a]["used"] if e in keep]
            edges += [{"source": a, "target": e, "kind": "generated"} for e in acts[a]["generated"] if e in keep]
    return {"nodes": nodes, "edges": edges, "truncated": len(o["entities"]) > cap or len(o["activities"]) > cap}


def index_dir_of(pid: str) -> str:
    d = P.pdir(pid) / "tv-index"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def ask(pipeline: str, question: str, steps: List[Dict[str, Any]], synthesis: str = "", chose: str = "agent", chat: str = "") -> Dict[str, Any]:
    """Answer a question with a plan: query, compute, check the synthesis, keep the record in the pipeline's answers.jsonl."""
    from . import answers as A
    c = P.load(pipeline)
    tr = A.traces_of(c)
    if not tr:
        raise PlanError("this pipeline has no traces yet")
    r = run(tr, steps, index_dir_of(pipeline))
    syn = check_synthesis(synthesis, len(steps), r["computed"])
    last = r["steps"][-1]
    rec = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 1), "pipeline": pipeline, "chat": chat, "engine": "traceviewer", "question": question,
           "plan": {"steps": steps, "chosen_by": chose}, "steps": r["steps"], "queries": r["queries"], "reads": r["reads"], "engine_info": r["engine"],
           "result": {"headline": last["headline"], "table": last["table"]}, "synthesis": synthesis, "synthesis_check": syn,
           "result_hash": r["result_hash"], "secs": r["secs"], "lineage": r["graph"], "stats": r["stats"],
           "direction": next((s.get("direction") for s in steps if s.get("op") == "lineage"), None),
           "verified": True, "problems": syn["problems"], "answer": synthesis or last["headline"], "targets": (r["outs"][0].get("ids") or [])[:20]}
    with A._LOCK:
        with open(P.pdir(pipeline) / "answers.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    return rec


def rerun(pipeline: str, aid: str) -> Dict[str, Any]:
    """Repeat a kept answer's plan on the traces as they are now: the same result hash, or which files changed."""
    from . import answers as A
    rec = next((a for a in A.listing(pipeline) if a.get("id") == aid), None)
    if not rec or rec.get("engine") != "traceviewer":
        raise PlanError("no computed answer with that id")
    r = run(A.traces_of(P.load(pipeline)), rec["plan"]["steps"], index_dir_of(pipeline))
    was = {x["file"]: x for x in rec["reads"]}
    changed = [x["file"] for x in r["reads"] if (was.get(x["file"]) or {}).get("sha256_ends") != x["sha256_ends"] or (was.get(x["file"]) or {}).get("bytes") != x["bytes"]]
    return {"same": r["result_hash"] == rec["result_hash"], "result_hash": r["result_hash"], "was": rec["result_hash"], "files_changed": changed,
            "headline": r["steps"][-1]["headline"], "secs": r["secs"], "queries": len(r["queries"])}


def synthesize(pipeline: str, aid: str, text: str) -> Dict[str, Any]:
    """Attach the agent's write-up to a computed answer, checked against it: every [Sk] a step, every number in the computed result."""
    from . import answers as A
    rec = next((a for a in A.listing(pipeline, limit=10 ** 6) if a.get("id") == aid), None)
    if not rec or rec.get("engine") != "traceviewer":
        raise PlanError("no computed answer with that id: evidence_answer first")
    computed = "\n".join(s["headline"] + "\n" + s["table"] for s in rec["steps"])
    syn = check_synthesis(text, len(rec["steps"]), computed)
    rec = {**rec, "synthesis": text, "synthesis_check": syn, "problems": syn["problems"], "answer": text or rec["result"]["headline"], "updated": round(time.time(), 1)}
    with A._LOCK:
        with open(P.pdir(pipeline) / "answers.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    return {"id": aid, "synthesis_check": syn}
