"""The domain scientist's Workbench: campaigns (run pipelines, watched live) and datasets (provenance they already have,
asked with chat). Both are pipelines underneath; this module is the plain view of them.

- items(): every campaign and dataset of the workspace, newest first, with its chat
- open_dataset(): a path the user picks (any folder, file or glob; it changes per analysis) -> an analysis pipeline,
  linked to the dashboard, with its own chat
- card(): what a dataset holds and the questions it can answer
- live(): a campaign's provenance while it is written - counts, rates, the newest artifacts, stall time - read
  incrementally (provtrace.Tail), so a 12-hour run costs one pass, not one per poll
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import jobs, pipeline as P, provtrace as T

STALL_S = float(os.environ.get("MOIRAI_AGENTS_STALL_S", "900"))     # no new provenance for this long while running: stalled
LIVE_EVERY_S = 10.0       # a watched campaign's traces are read this often, in the background
LIVE_IDLE_S = 300.0       # and no longer once no page has asked for this long


def _traces(c: Dict[str, Any]) -> str:
    return (c.get("dashboard") or {}).get("traces") or c.get("traces") or ""


def _state(c: Dict[str, Any], js: List[dict]) -> str:
    st = [s["status"] for s in c["stages"].values()]
    if any(j.get("status") == "running" for j in js) or "running" in st:
        return "running"
    if "needs_user" in st:
        return "needs_you"
    if "failed" in st:
        return "failed"
    if all(s in ("passed", "skipped") for s in st):
        return "done"
    return "idle"


def _running_jobs(pid: str, js: List[dict], ws: str) -> List[dict]:
    """The pipeline's running jobs: name, elapsed seconds, and the last line of the log (what it is doing now)."""
    out = []
    for j in js:
        if j.get("status") != "running":
            continue
        last = ""
        try:
            tail = (jobs.get(pid, j["id"], 600, ws) or {}).get("log_tail", "")
            last = next((x.strip() for x in reversed(tail.splitlines()) if x.strip()), "")[:200]
        except Exception:  # noqa: BLE001
            pass
        out.append({"id": j.get("id"), "name": j.get("name"), "stage": j.get("stage"), "elapsed": j.get("elapsed"), "last": last})
    return out[:6]


# ------------------------------------------------------------------ published versions of an app: every baseline it ever published
PUB_CACHE_S = 60.0
_PUB: Dict[str, tuple] = {}


def _family(name: str) -> str:
    return re.sub(r"-v\d+$", "", name or "")


def published_runs(c: Dict[str, Any]) -> List[dict]:
    """The baseline runs an app published under its name, one per version (name, name-v2, ... name-vN), oldest first, read from
    <publish_root>/provenance-traces/<name>/<timestamp>/run.json; a version published without baseline traces is not listed.
    Cached for a minute: the publish root is on a shared file system."""
    pk = (c.get("stages", {}).get("package") or {}).get("outputs") or {}
    root, app = pk.get("publish_root"), pk.get("publish_app")
    if not root or not app:
        return []
    fam = _family(app)
    key = f"{root}|{fam}"
    hit = _PUB.get(key)
    if hit and time.time() - hit[0] < PUB_CACHE_S:
        return hit[1]
    rx = re.compile(re.escape(fam) + r"(?:-v(\d+))?$")
    out, seen = [], {}
    traces_root = Path(root) / "provenance-traces"
    names = set()
    for base in (traces_root,):
        try:
            names |= {d.name for d in base.iterdir() if d.is_dir() and rx.fullmatch(d.name)}
        except OSError:
            pass
    for name in sorted(names, key=lambda n: int(rx.fullmatch(n).group(1) or 1)):      # oldest first: a re-published run points back to its first
        ver = int(rx.fullmatch(name).group(1) or 1)
        runs = []
        try:
            stamps = sorted(d for d in (traces_root / name).iterdir() if d.is_dir())
        except OSError:
            stamps = []
        for d in stamps:
            try:
                meta = json.loads((d / "run.json").read_text())
            except (OSError, ValueError):
                meta = {}
            if not T.expand(str(d / "raw")):
                continue
            r = meta.get("run") or {}
            kind = r.get("kind") or ("verification" if (d / "verification.json").exists() else "baseline")
            item = {"name": name, "version": ver, "kind": kind, "ts": d.name, "path": str(d / "raw"), "scale": r.get("scale") or "",
                    "input": r.get("input") or "", "result": r.get("result") or "", "commit": (meta.get("app") or {}).get("source_commit", "")[:10],
                    "label": f"v{ver} {kind}"}
            same = seen.get((kind, d.name))
            if same:
                item["same_as"] = f"v{same}"           # one run published again under a newer name
            else:
                seen[(kind, d.name)] = ver
            runs.append(item)
        best = next((r for r in reversed(runs) if r["kind"] == "baseline"), None)       # one per version: its published baseline traces
        if best:
            best["label"] = f"v{ver}"
            out.append(best)
    out.sort(key=lambda x: x["version"])
    _PUB[key] = (time.time(), out)
    return out


def open_published(ws: str, app: str, path: str, chat: str) -> Dict[str, Any]:
    """The dataset of one published run of ``app``: made on first use, the same one after."""
    c = P.load(app, ws)
    run = next((r for r in published_runs(c) if r["path"] and r["path"] == path), None)
    if not run:
        raise P.PipelineError(f"{path} is not a published run of {c['name']}")
    for x in P.list_all("", ws):
        if x.get("kind") == "analysis" and x.get("parent") == app and (x.get("published") or {}).get("path") == path:
            return x
    d = open_dataset(ws, path, f"{run['name']}-{run['kind'][:6]}-{run['ts']}", chat)

    def tie(x):
        x["parent"], x["published"] = app, {k: run.get(k) for k in ("name", "version", "kind", "ts", "path", "scale", "input", "commit", "label")}
    P.update(d["id"], tie)
    return P.load(d["id"], ws)


def items(ws: str, busy: Optional[set] = None) -> Dict[str, List[dict]]:
    """apps: run pipelines (the Annotate view), lives: live runs (the Live Run view, each under its app), datasets: analysis
    pipelines; every one of them can be asked in the Query view. campaigns = apps, for older pages."""
    out = {"apps": [], "lives": [], "datasets": [], "published": []}
    for c in P.list_all("", ws):
        js = jobs.list_jobs(c["id"], ws)
        cur = next((n for n in P.KINDS.get(c["kind"], c["stages"]) if n in c["stages"] and c["stages"][n]["status"] not in ("passed", "skipped")), None)
        row = {"id": c["id"], "name": c["name"], "kind": c["kind"], "chat": c.get("chat_session") or "",
               "created": c.get("created"), "updated": c.get("updated") or c.get("created"), "current": cur,
               "state": _state(c, js), "traces": _traces(c), "live": bool((c.get("dashboard") or {}).get("live")),
               "dash_run": P.dash_run(c, ws), "jobs_running": sum(1 for j in js if j.get("status") == "running"),
               "steps": [{"name": n, "status": c["stages"][n]["status"]} for n in P.KINDS.get(c["kind"], c["stages"]) if n in c["stages"]],
               "summary": (c["stages"].get(cur or "", {}).get("outputs") or {}).get("summary", ""),
               "parent": c.get("parent") or "", "query_chat": c.get("query_chat") or "",
               "jobs": _running_jobs(c["id"], js, ws)}
        if busy and row["chat"] in busy:          # the agent is working in its chat: a gate it asked about is being answered or acted on
            row["busy"] = True
            if row["state"] in ("needs_you", "idle"):
                row["state"] = "working"
        if c["kind"] == "run":
            pk, gr = c["stages"]["package"]["outputs"], c["stages"]["graph"]["outputs"]
            row.update(published=pk.get("published_package") or "", diff=((gr.get("health") or {}).get("diff") or {}).get("summary"),
                       can_live=c["stages"]["integrate"]["status"] == "passed", live_inputs=c.get("live_inputs"),
                       gaps=[{**{k: g.get(k) for k in ("id", "kind", "subject", "detail", "status", "cause", "how", "generic", "workload", "opened", "closed", "recheck")},
                              "question": (g.get("from") or {}).get("question", ""), "seen": 1 + len(g.get("seen_in") or [])} for g in c.get("gaps") or []])
            row["versions"] = published_runs(c)
        elif c["kind"] == "analysis" and c.get("parent") and c.get("published"):
            row.update(published_run=c["published"])
        elif c["kind"] == "live":
            row.update(published=c["stages"]["publish_data"]["outputs"].get("published_live") or "",
                       started=c["stages"]["live_run"].get("started"), command=c.get("command", ""), inputs=c.get("inputs") or {})
        out[{"run": "apps", "live": "lives"}.get(c["kind"], "published" if row.get("published_run") else "datasets")].append(row)
    names = {a["id"]: a["name"] for a in out["apps"]}
    for x in out["lives"] + out["published"]:
        x["app"] = names.get(x["parent"], x["parent"])
    opened = {(x["parent"], x["published_run"]["path"]): x["id"] for x in out["published"]}
    for a in out["apps"]:
        for r in a.get("versions") or []:
            if (a["id"], r["path"]) in opened:
                r["pipeline"] = opened[(a["id"], r["path"])]
    out["campaigns"] = out["apps"]
    return out


def new_live(ws: str, parent: str, name: str, command: str, chat: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A live run of an annotated app (a live pipeline under ``parent``), owned by chat ``chat``. With ``values`` (the app's live-run
    inputs, picked in the Live Run view) the command is rendered from the app's template; otherwise ``command`` is used as given."""
    from ..core import liveinputs as LI
    spec = P.load(parent, ws).get("live_inputs")
    if values is not None and spec:
        r = LI.render(spec, values)
        if r["status"] != "ok":
            raise P.PipelineError("; ".join(r["problems"]))
        command, values = r["command"], r["values"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-.")[:40]
    if not name:
        name = (P.load(parent, ws)["name"] + "-live")[:40]
    if not name[0].isalnum():
        name = "r" + name
    c = P.new("live", name, command=command or "", parent=parent, notes="started from the Live Run view")

    def own(x):
        x["chat_session"] = chat
        if values is not None and spec:
            x["inputs"] = values
    P.update(c["id"], own)
    return P.load(c["id"], ws)


def open_dataset(ws: str, path: str, name: str, chat: str) -> Dict[str, Any]:
    """An analysis pipeline over traces at ``path``, linked where dfdashboard watches, owned by chat ``chat``."""
    path = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not T.expand(path):
        raise P.PipelineError(f"no .pfw.gz trace under {path}")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name or os.path.basename(path.rstrip("/*")) or "data").strip("-.")[:40] or "data"
    if not name[0].isalnum():
        name = "d" + name
    c = P.new("analysis", name, traces=path, notes="opened from the Workbench")
    raw = P.pdir(c["id"]) / "traces" / "analysis" / "raw"
    P.link_traces(path, raw)
    P.set_dashboard(c["id"], str(raw), False)

    def own(x):
        x["chat_session"] = chat
    P.update(c["id"], own)
    return P.load(c["id"], ws)


# ------------------------------------------------------------------ data card + suggested questions
_CARDS: Dict[str, tuple] = {}


def _stamp(traces: str) -> tuple:
    fs = T.expand(traces)
    return tuple(sorted((f, os.path.getsize(f)) for f in fs if os.path.exists(f)))


def card(ws: str, pid: str) -> Dict[str, Any]:
    c = P.load(pid, ws)
    t = _traces(c)
    stamp = _stamp(t)
    hit = _CARDS.get(pid)
    if hit and hit[0] == stamp:
        return hit[1]
    g = T.read(t)
    s = g.summary()
    ts = [a["ts"] for a in g.activities if isinstance(a.get("ts"), (int, float))]
    span = (max(a["ts"] + (a.get("dur") or 0) for a in g.activities if isinstance(a.get("ts"), (int, float))) - min(ts)) / 1e6 if ts else 0
    roles = {x["type"]: x.get("role", "") for x in s["types"]}
    outputs = [k for k, _ in Counter(s["by_type"]).most_common() if roles.get(k) in ("output", "artifact", "result")] or \
              [k for k, _ in Counter(s["by_type"]).most_common()]
    inputs = [k for k in s["by_type"] if roles.get(k) == "input"]
    acts = [a["name"] for a in s["activity_kinds"]]
    q = []
    if outputs:
        q.append(f"Where did the {outputs[0]} entities come from? Show the lineage of one of them.")
    if inputs:
        q.append(f"Which outputs were produced from each {inputs[0]}?")
    if acts:
        q.append(f"Which activities took the most time, and how much in total? Start with {acts[0]}.")
    q.append("How many entities of each type were produced over time? Show it per hour.")
    if len(acts) > 1:
        q.append(f"What did {acts[1]} use and what did it generate?")
    q.append("Is anything incomplete: entities that were used but never produced, or activities without outputs?")
    out = {"id": pid, "name": c["name"], "traces": c.get("traces"), "files": len(stamp), "bytes": sum(x[1] for x in stamp),
           "counts": {**{k: s[k] for k in ("activities", "entities", "edges", "relations", "dangling")}, "types": len(s["types"])},
           "span_s": round(span, 1), "by_type": s["by_type"], "types": s["types"],
           "activity_kinds": [{"name": a["name"], "count": a["count"], "seconds": round(a["total_us"] / 1e6, 1)} for a in s["activity_kinds"][:20]],
           "questions": q, "dash_run": P.dash_run(c, ws)}
    _CARDS[pid] = (stamp, out)
    return out


# ------------------------------------------------------------------ live campaign
class _Live:
    def __init__(self, traces: str) -> None:
        self.traces = traces
        self.tail = T.Tail(traces)
        self.g = T.Graph()
        self.feed: deque = deque(maxlen=40)      # newest artifacts, with what produced them
        self.samples: deque = deque(maxlen=720)  # (time, entities, activities) every poll, for rates
        self.last_new = 0.0
        self.lock = threading.Lock()
        self.seen = time.time()                  # last time a page asked: the reader stops when nobody watches
        self.ready = False
        threading.Thread(target=self._run, daemon=True, name="live-provenance").start()

    def _run(self) -> None:
        while time.time() - self.seen < LIVE_IDLE_S:
            caught = True
            try:
                caught = self.poll()
            except Exception as e:  # noqa: BLE001  a bad member must not end the reader
                print(f"live provenance read failed: {e}", flush=True)
            self.ready = self.ready or caught
            if caught:
                time.sleep(LIVE_EVERY_S)          # behind (a campaign opened late): keep reading
        _LIVE.pop(next((k for k, v in _LIVE.items() if v is self), None), None)

    def poll(self, budget: float = 8.0) -> bool:
        """Read what is new, for at most ``budget`` seconds. True when it reached the end of what is written."""
        end = time.time() + budget
        while time.time() < end:
            if not self._poll_once():
                return True
        return False

    def _poll_once(self) -> bool:
        recs = self.tail.poll()                  # the slow part (reading files): outside the lock
        with self.lock:
            self.g.set_files(self.tail.files)
        if not recs:
            return False
        with self.lock:
            made = {}
            for d in self.g.extend(recs):
                if d["kind"] == "activity":
                    gen = (d.get("relations") or {}).get("generated")
                    for x in gen if isinstance(gen, list) else ([gen] if gen else []):
                        made[x] = d["name"]
            for eid, act in made.items():
                e = self.g.entities.get(eid, {"id": eid})
                self.feed.appendleft({"id": eid, "type": e.get("type", ""), "uri": e.get("uri", ""), "by": act, "seen": time.time()})
            self.last_new = time.time()
            return True


_LIVE: Dict[str, _Live] = {}


def _reader(pid: str, traces: str) -> _Live:
    lv = _LIVE.get(pid)
    if lv is None or lv.traces != traces:
        lv = _LIVE[pid] = _Live(traces)
    return lv


def live(ws: str, pid: str) -> Dict[str, Any]:
    c = P.load(pid, ws)
    t = _traces(c)
    if not t:
        return {"id": pid, "status": "no traces yet", "counts": {}, "feed": []}
    js = jobs.list_jobs(pid, ws)
    state = _state(c, js)
    if state != "running":                    # finished: the counts come from TraceViewer (cached), no reader keeps the graph in Python
        agg = _tv_aggregate(c, ws)
        n = agg["counts"] if agg else {}
        by_type = Counter({x["label"]: x.get("count", 0) for x in (agg or {}).get("nodes", []) if x.get("kind") == "type"})
        return {"id": pid, "name": c["name"], "state": state, "counts": n, "rate": {"entities_per_h": None, "activities_per_h": None},
                "by_type": dict(by_type.most_common(12)), "feed": [], "quiet_s": None, "stalled": False,
                "jobs": [{k: j.get(k) for k in ("id", "name", "status", "elapsed", "rc")} for j in js][:12],
                "dash_run": P.dash_run(c, ws), "live": bool((c.get("dashboard") or {}).get("live")), "reading": agg is None}
    lv = _reader(pid, t)
    now = lv.seen = time.time()
    with lv.lock:
        n = lv.g.counts()
        lv.samples.append((now, n["entities"], n["activities"]))
        old = next((s for s in lv.samples if now - s[0] <= 3600), lv.samples[0])
        hrs = max((now - old[0]) / 3600, 1e-9)
        rate = {"entities_per_h": round((n["entities"] - old[1]) / hrs, 1) if now - old[0] > 60 else None,
                "activities_per_h": round((n["activities"] - old[2]) / hrs, 1) if now - old[0] > 60 else None}
        by_type = Counter(e["type"] for e in lv.g.entities.values())
        feed = list(lv.feed)[:20]
    quiet = now - lv.last_new if lv.last_new else None
    return {"id": pid, "name": c["name"], "state": state, "counts": n, "rate": rate, "by_type": dict(by_type.most_common(12)),
            "feed": feed, "quiet_s": round(quiet, 1) if quiet is not None else None,
            "stalled": bool(state == "running" and quiet is not None and quiet > STALL_S),
            "jobs": [{k: j.get(k) for k in ("id", "name", "status", "elapsed", "rc")} for j in js][:12],
            "dash_run": P.dash_run(c, ws), "live": bool((c.get("dashboard") or {}).get("live")), "reading": not lv.ready}


# ------------------------------------------------------------------ the Query view's graph
GRAPH_CAP = int(os.environ.get("MOIRAI_AGENTS_QUERY_GRAPH_CAP", "1500"))     # nodes; a larger graph shows only the selected answer's part
_GREAD: Dict[str, threading.Thread] = {}


def _nodes(g: "T.Graph") -> Dict[str, Any]:
    """Entities and activities, each with its captured values as properties (an entity's attributes, an activity's inputs and outputs, as
    name=value); the captured value entities themselves are not drawn."""
    from ..core import aggregate as G
    fo = G.fold(g)
    cap = {i for i, e in g.entities.items() if G.captured_type(e.get("type", ""))}
    ents = [{"id": e["id"], "kind": "entity", "type": e.get("type", ""), "uri": e.get("uri", ""), "attrs": fo["attrs"].get(e["id"], []),
             "label": ((e.get("uri") or e["id"]).rstrip("/").rsplit("/", 1)[-1] or e["id"])[:40]} for e in g.entities.values() if e["id"] not in cap]
    acts = [{"id": a["id"], "kind": "activity", "label": a["name"], "pid": a.get("pid"), "ts": a.get("ts"), "dur": a.get("dur"),
             "inputs": fo["inputs"].get(a["id"], []), "outputs": fo["outputs"].get(a["id"], [])} for a in g.activities]
    edges = [({"source": e["entity"], "target": e["activity"]} if e["kind"] == "used" else {"source": e["activity"], "target": e["entity"]}) | {"kind": e["kind"]}
             for e in g.edges if e["entity"] not in cap]
    edges += [{"source": r["subject"], "target": r["object"], "kind": r["relation"], "relation": True} for r in g.relations if r.get("object") and r["subject"] not in cap]
    return {"nodes": ents + acts, "edges": edges}


def _source(c: Dict[str, Any]):
    """(lock, graph) of a pipeline's provenance, or None while it is still being read. A live run (or a run pipeline whose
    run is being watched) uses its live reader, the same one the Live Run view polls; anything else is read in the
    background through answers.graph, which reads only what is new."""
    from ..core import answers as A
    pid = c["id"]
    if c["kind"] in ("run", "live") and _traces(c) and (c["kind"] == "live" or _traces(c) == A.traces_of(c)):   # the run being watched
        lv = _reader(pid, _traces(c))
        lv.seen = time.time()
        return (lv.lock, lv.g) if lv.ready else None
    if not A.traces_of(c):
        return (threading.Lock(), T.Graph())
    t = _GREAD.get(pid)
    if t is not None and t.is_alive():
        return None
    t = _GREAD[pid] = threading.Thread(target=A.graph, args=(c,), daemon=True, name="query-graph")
    t.start()
    t.join(timeout=2.0)
    r = A._READERS.get(pid)
    return None if t.is_alive() or r is None else (r.lock, r.g)


def aggregate(ws: str, pid: str) -> Dict[str, Any]:
    """The aggregated (entity-relationship) graph of a pipeline: entity types and activity kinds, with counts. With an
    annotation spec (a run, or a live run's parent) it is the diff against what the spec promised; before anything ran,
    only what the spec promises."""
    from ..core import aggregate as G
    c = P.load(pid, ws)
    spec = None
    if c["kind"] in ("run", "live"):
        spec = P.stage(P.origin(c), "annotate")["outputs"].get("spec") or None
    exp = G.expected(spec) if spec else None
    from ..core import answers as A
    if not _traces(c) and not A.traces_of(c):
        if exp is None:
            return {"id": pid, "mode": "none", "nodes": [], "edges": []}
        return {"id": pid, "mode": "expected", "nodes": [{**n, "status": "expected"} for n in exp["nodes"]],
                "edges": [{**e, "status": "conditional" if e.get("conditional") else "expected"} for e in exp["edges"]]}
    got = _tv_aggregate(c, ws)
    if got is None:
        return {"id": pid, "reading": True, **({"mode": "expected", "nodes": exp["nodes"], "edges": exp["edges"]} if exp else {})}
    n, run = got["counts"], {"nodes": got["nodes"], "edges": got["edges"]}
    extra = {"updating": True} if got.get("stale") else {}
    if exp is None:
        return {"id": pid, "mode": "runtime", "counts": n, **run, **extra}
    return {"id": pid, "mode": "diff", "counts": n, **G.diff(exp, run), **extra}


_AGG_RUN: Dict[str, threading.Thread] = {}


def _tv_aggregate(c: Dict[str, Any], ws: str) -> Optional[Dict[str, Any]]:
    """The aggregated graph of a pipeline's traces, computed by TraceViewer (aggregate.tv_runtime) and kept in <pipeline>/aggregate.json with
    the size and mtime of every trace file: the same files return it at once (also after a restart); changed files (a run still writing)
    return the last one marked stale while a background thread computes the new one. None until the first is ready."""
    from ..core import aggregate as G, answers as A
    traces = _traces(c) or A.traces_of(c)
    files = T.expand(traces)
    key = [[f, *(T._stamp(f) or (0, 0))] for f in files]
    path = P.pdir(c["id"], ws) / "aggregate.json"
    try:
        kept = json.loads(path.read_text())
    except (OSError, ValueError):
        kept = None
    if kept and kept.get("key") == key:
        return kept["run"]
    t = _AGG_RUN.get(c["id"])
    if t is None or not t.is_alive():
        def work():
            try:
                run = G.tv_runtime(traces)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"key": key, "run": run, "ts": round(time.time(), 1)}))
                os.replace(tmp, path)
            except Exception as e:  # noqa: BLE001  shown as still reading; the next poll tries again
                print(f"aggregate {c['id']} failed: {type(e).__name__}: {e}", flush=True)
        t = _AGG_RUN[c["id"]] = threading.Thread(target=work, daemon=True, name="tv-aggregate")
        t.start()
        t.join(timeout=2.0)
        if not t.is_alive():
            return _tv_aggregate(c, ws) if path.exists() and json.loads(path.read_text()).get("key") == key else None
    return {**kept["run"], "stale": True} if kept else None


def graph(ws: str, pid: str, since: str = "") -> Dict[str, Any]:
    """The whole provenance graph of a campaign or dataset, for the Query view, which highlights the part a query selected.
    A campaign being watched uses its live reader; otherwise the graph is read in the background (``reading`` until done).
    ``since``: the signature the page already has; the same one returns {same: true}, so a poll costs nothing."""
    c = P.load(pid, ws)
    agg = _tv_aggregate(c, ws)            # TraceViewer counts (cached): a graph too big to draw is never built in Python
    if agg is None:
        return {"id": pid, "reading": True}
    n = agg["counts"]
    if n["entities"] + n["activities"] > GRAPH_CAP:
        sig = f"{n['entities']}:{n['activities']}:{n['edges']}:{n['relations']}"
        if since and since == sig:
            return {"id": pid, "same": True, "sig": sig}
        return {"id": pid, "sig": sig, "counts": n, "truncated": True}
    src = _source(c)
    if src is None:
        return {"id": pid, "reading": True}
    lock, g = src
    with lock:
        n = g.counts()
        sig = f"{n['entities']}:{n['activities']}:{n['edges']}:{n['relations']}"
        if since and since == sig:
            return {"id": pid, "same": True, "sig": sig}
        total = n["entities"] + n["activities"]
        out = {"id": pid, "sig": sig, "counts": n, "truncated": total > GRAPH_CAP}
        if total <= GRAPH_CAP:
            out.update(_nodes(g))
    return out


def new_chat_id() -> str:
    return uuid.uuid4().hex[:12]
