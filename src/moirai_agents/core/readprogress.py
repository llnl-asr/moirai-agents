"""Progress and timings of trace reads: what step a read of a pipeline's traces is on, how far it is, and how long each step took.

A read (opening a dataset, the aggregated diagram, the Query view's graph, a live run's catch-up, an answer's queries, a live run's
re-index) is an ``op``; the readers in provtrace / aggregate / evidence mark their ``step``s inside it. A step outside any op costs
nothing. Ops of every process (the web UI and the MCP server) are shared through files, so the web UI's progress popup shows them all:

  <state>/reads/active/<pipeline>/<op id>.json   the op while it runs (rewritten at most every WRITE_EVERY_S, removed when it ends)
  <state>/reads/last/<pipeline>.json             the last finished op of each kind
  <state>/reads/timings.jsonl                    one line per finished op that did work: its steps and their times, to optimize reads
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..env import state_dir

WRITE_EVERY_S = 0.5
SHOW_AFTER_S = 0.5           # an op that only checked a fresh index and ended sooner is neither shown nor logged
QUIET_STEPS = {"find files", "check index", "wait for the index"}
_tl = threading.local()


def _root() -> Path:
    return state_dir("reads")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "none")[:120]


class Step:
    def __init__(self, op: "Op", name: str, total: Optional[int], unit: str, nbytes: Optional[int], detail: str, depth: int) -> None:
        self.op, self.name, self.total, self.unit, self.bytes, self.detail, self.depth = op, name, total, unit, nbytes, detail, depth
        self.done, self.bytes_done, self.t0, self.t1, self.status = 0, 0, time.time(), None, "running"

    def advance(self, n: int = 1, detail: str = "", nbytes: int = 0) -> None:
        self.done += n
        self.bytes_done += nbytes
        if detail:
            self.detail = detail
        self.op.write()

    def note(self, detail: str) -> None:
        self.detail = detail
        self.op.write()

    def as_dict(self) -> Dict[str, Any]:
        end = self.t1 or time.time()
        return {"name": self.name, "status": self.status, "done": self.done, "total": self.total, "unit": self.unit, "bytes": self.bytes,
                "bytes_done": self.bytes_done, "detail": self.detail[:200], "depth": self.depth, "start": round(self.t0, 3),
                "secs": round(end - self.t0, 3)}


class _Null:
    def advance(self, *a, **k) -> None:
        pass

    def note(self, *a, **k) -> None:
        pass


class Op:
    def __init__(self, pipeline: str, kind: str, traces: str = "", log_min_s: float = 0.0) -> None:
        self.pipeline, self.kind, self.traces, self.log_min_s = pipeline or "", kind, traces, log_min_s
        self.id = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        self.t0, self.steps, self.status, self.error = time.time(), [], "running", ""
        self.depth, self.worked, self._written = 0, False, 0.0
        self.files, self.bytes = 0, 0

    @property
    def visible(self) -> bool:
        return self.worked or time.time() - self.t0 >= SHOW_AFTER_S

    def _active(self) -> Path:
        return _root() / "active" / _safe(self.pipeline) / f"{self.id}.json"

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "pid": os.getpid(), "pipeline": self.pipeline, "kind": self.kind, "status": self.status, "error": self.error[:300],
                "start": round(self.t0, 3), "secs": round(time.time() - self.t0, 3), "files": self.files, "bytes": self.bytes,
                "steps": [s.as_dict() for s in self.steps], "updated": round(time.time(), 3)}

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not self.visible or (not force and now - self._written < WRITE_EVERY_S):
            return
        self._written = now
        _atomic(self._active(), self.as_dict())

    def finish(self, status: str, error: str = "") -> None:
        self.status, self.error = status, error
        for s in self.steps:
            if s.t1 is None:
                s.t1, s.status = time.time(), status
        try:
            self._active().unlink()
        except OSError:
            pass
        secs = time.time() - self.t0
        if not error and (not (self.worked or secs >= SHOW_AFTER_S) or secs < self.log_min_s):
            return
        d = self.as_dict()
        _append(_root() / "timings.jsonl", d)
        last = _root() / "last" / f"{_safe(self.pipeline)}.json"
        kept = _read(last) or {}
        kept[self.kind] = d
        _atomic(last, kept)


def _atomic(p: Path, obj: Any) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(obj))
        os.replace(tmp, p)
    except OSError:
        pass


def _append(p: Path, obj: Any) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(obj) + "\n")
    except OSError:
        pass


def _read(p: Path) -> Optional[Any]:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def current() -> Optional[Op]:
    return getattr(_tl, "op", None)


@contextmanager
def op(pipeline: str, kind: str, traces: str = "", log_min_s: float = 0.0) -> Iterator[Op]:
    """A read of a pipeline's traces, on this thread: the steps marked inside it are its progress. Nested in another op, it is that op.
    log_min_s: a read that ends sooner (without error) is not logged (a live run's routine poll)."""
    outer = current()
    if outer is not None:
        yield outer
        return
    o = _tl.op = Op(pipeline, kind, traces, log_min_s)
    try:
        yield o
        o.finish("done")
    except BaseException as e:
        o.finish("failed", f"{type(e).__name__}: {e}")
        raise
    finally:
        _tl.op = None


@contextmanager
def step(name: str, total: Optional[int] = None, unit: str = "files", nbytes: Optional[int] = None, detail: str = "") -> Iterator[Any]:
    """One step of the current op (nothing without one): yields a handle to advance(n, detail, nbytes) as work completes."""
    o = current()
    if o is None:
        yield _Null()
        return
    s = Step(o, name, total, unit, nbytes, detail, o.depth)
    o.steps.append(s)
    if name not in QUIET_STEPS:
        o.worked = True
    o.depth += 1
    o.write(force=True)
    try:
        yield s
        s.status = "done"
    except BaseException:
        s.status = "failed"
        raise
    finally:
        o.depth -= 1
        s.t1 = time.time()
        o.write(force=True)


def record(name: str, secs: float, detail: str = "") -> None:
    """A step that ran elsewhere (a worker process) and is known only by its time: added to the current op as done, for the timings log."""
    o = current()
    if o is None:
        return
    s = Step(o, name, None, "", None, detail, o.depth)
    s.t0, s.t1, s.status = time.time() - secs, time.time(), "done"
    o.steps.append(s)
    o.write()


def set_size(files: int, nbytes: int) -> None:
    o = current()
    if o is not None:
        o.files, o.bytes = files, nbytes


# ------------------------------------------------------------------ what the web UI shows
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def active(pipeline: str) -> List[Dict[str, Any]]:
    """The reads of this pipeline running now, in any process, with an estimate for each running step that has a byte size."""
    out = []
    d = _root() / "active" / _safe(pipeline)
    rates = None
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        o = _read(p)
        if not o:
            continue
        if not _alive(int(o.get("pid") or 0)):
            try:
                p.unlink()                         # its process is gone (killed mid-read)
            except OSError:
                pass
            continue
        now = time.time()
        o["secs"] = round(now - o["start"], 3)
        o["since_update"] = round(now - o.get("updated", now), 1)
        for s in o["steps"]:
            if s["status"] == "running":
                s["secs"] = round(now - s["start"], 3)
                if s.get("bytes"):
                    rates = rates if rates is not None else step_rates()
                    r = rates.get(s["name"])
                    if r:
                        left = max(0.0, (s["bytes"] - s.get("bytes_done", 0)) / r - (s["secs"] if not s.get("bytes_done") else 0))
                        s["eta_s"] = round(left, 1)
                        s["rate_mb_s"] = round(r / 1e6, 1)
        out.append(o)
    return out


def recent(pipeline: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """The last finished reads (of one pipeline, or all), newest last."""
    p = _root() / "timings.jsonl"
    try:
        with open(p) as fh:
            lines = fh.readlines()[-5000:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if not pipeline or o.get("pipeline") == pipeline:
            out.append(o)
    return out[-limit:]


def step_rates(limit: int = 200) -> Dict[str, float]:
    """Median bytes per second of each step that carries a byte size, over the recent reads: the estimate for a step under way."""
    by: Dict[str, List[float]] = {}
    for o in recent(limit=limit):
        for s in o.get("steps", []):
            if s.get("bytes") and s.get("secs", 0) > 0.2 and s.get("status") == "done":
                by.setdefault(s["name"], []).append(s["bytes"] / s["secs"])
    return {k: statistics.median(v) for k, v in by.items() if v}


def stats(pipeline: str = "", limit: int = 50) -> Dict[str, Any]:
    """Where reads spend their time, to optimize them: per kind of read and per step, how often, median and worst seconds, share of the
    total, and the throughput of steps with a byte size."""
    ops = recent(pipeline, limit)
    kinds: Dict[str, List[float]] = {}
    steps: Dict[tuple, Dict[str, Any]] = {}
    total = sum(o.get("secs", 0) for o in ops) or 1e-9
    for o in ops:
        kinds.setdefault(o["kind"], []).append(o.get("secs", 0))
        for s in o.get("steps", []):
            if s.get("depth", 0) > 1:
                continue
            k = (o["kind"], s["name"])
            d = steps.setdefault(k, {"kind": o["kind"], "step": s["name"], "secs": [], "bytes": 0, "bytes_secs": 0.0})
            d["secs"].append(s.get("secs", 0))
            if s.get("bytes"):
                d["bytes"] += s["bytes"]
                d["bytes_secs"] += s.get("secs", 0)
    rows = []
    for d in steps.values():
        xs = d.pop("secs")
        bs, bsecs = d.pop("bytes"), d.pop("bytes_secs")
        rows.append({**d, "count": len(xs), "median_s": round(statistics.median(xs), 3), "max_s": round(max(xs), 3),
                     "total_s": round(sum(xs), 3), "share": round(sum(xs) / total, 3),
                     "mb_s": round(bs / bsecs / 1e6, 1) if bsecs > 0 else None})
    rows.sort(key=lambda r: -r["total_s"])
    return {"reads": len(ops), "kinds": {k: {"count": len(v), "median_s": round(statistics.median(v), 3), "max_s": round(max(v), 3)} for k, v in kinds.items()},
            "steps": rows, "log": str(_root() / "timings.jsonl")}


def last(pipeline: str) -> Dict[str, Any]:
    return _read(_root() / "last" / f"{_safe(pipeline)}.json") or {}
