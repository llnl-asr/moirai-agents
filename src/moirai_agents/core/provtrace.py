"""Read moirai traces through dftracer-utils' TraceViewer, also while the service is writing them.

Nothing here parses a trace by hand: every file is indexed (``Indexer``, all cores, ``<trace dir>/.dftindex``) and queried with
``TraceViewer`` for the provenance records only (ET/EH/ER metadata and the activity events); every other event is counted by category
with a TraceViewer group-by. A file that changed since it was read (a run still writing) is re-indexed and read again; the graph
keeps one copy of each type, entity, relation and activity, so reading a record twice adds nothing. The index skips an unfinished
gzip member at the end of a file, so a member still being written is never misread.
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import readprogress as RP

ROLES = ["unknown", "input", "output", "intermediate", "parameter", "reference"]
STORES = ["memory", "gpu_memory", "local_disk", "parallel_fs", "burst_buffer", "object_store", "database", "network", "other"]
ENTITY_RELATIONS = {16: "derived_from", 17: "revision_of", 18: "contains", 19: "part_of", 20: "specialization_of",
                    21: "alternate_of", 22: "depends_on"}
ACTIVITY_CATS = ("moirai", "PROV")
PROV_NAMES = ("ET", "EH", "ER")


def expand(traces: str) -> List[str]:
    """A directory, a glob or a file -> the .pfw/.pfw.gz files in it (spill files are traces too)."""
    t = os.path.expanduser(traces)
    if os.pathsep in t and not os.path.exists(t):        # several paths: the exact files an answer read
        return sorted({f for part in t.split(os.pathsep) if part for f in expand(part)})
    # real paths: an index is keyed by absolute file path, so the same files reached through a symlinked directory (a pipeline's live_run/raw
    # -> the run's real traces dir) otherwise read a different, partial view of one index (74 entity records missing on 8.1M)
    if os.path.isdir(t):
        return sorted({os.path.realpath(f) for f in glob.glob(os.path.join(t, "**", "*.pfw*"), recursive=True)})
    return sorted({os.path.realpath(f) for f in glob.glob(t, recursive=True)})


def find(traces: str) -> List[str]:
    """expand(), marked as the read's first step (with the size of what it found) for the progress popup."""
    with RP.step("find files") as s:
        files = expand(traces)
        nbytes = sum((_stamp(f) or (0, 0))[0] for f in files)
        s.advance(len(files), f"{len(files)} file(s), {nbytes / 1e6:.0f} MB")
    RP.set_size(len(files), nbytes)
    return files


def index_dir(files: List[str]) -> str:
    """Where the TraceViewer index of these files lives: their (common) directory, ``<dir>/.dftindex``, when it is writable; else a
    per-user cache (MOIRAI_TV_INDEX_CACHE, default the system temp dir) keyed by that directory. An index is keyed by absolute file path."""
    if not files:
        return ""
    d = os.path.commonpath([os.path.dirname(os.path.abspath(f)) for f in files])
    if os.access(d, os.W_OK):
        return d
    base = os.environ.get("MOIRAI_TV_INDEX_CACHE") or os.path.join(tempfile.gettempdir(), f"moirai-tv-index-{os.getuid()}")
    c = os.path.join(base, hashlib.sha1(d.encode()).hexdigest()[:16])
    os.makedirs(c, exist_ok=True)
    return c


def tv():
    """dftracer-utils, the one TraceViewer every reader in moirai-agents uses (the prov_read capability): nothing else imports it."""
    return _tv()


def _tv():
    import dftracer.utils as U
    try:
        U.set_log_level("error")
    except Exception:  # noqa: BLE001
        pass
    return U


STAMPS = ".dftindex.moirai.json"     # beside <idx>/.dftindex: {file: {stamp: [size, mtime_ns], young, at}} of what the index was built from
LOCK = ".dftindex.moirai.lock"


@contextmanager
def _index_lock(idx: str):
    """One indexer at a time on an index, across threads and processes (the web UI's keeper, the MCP server's queries): a query that arrives
    while a live run's index is being rebuilt waits for it, then reads the fresh index. flock is per open file, so threads exclude each other
    too. A filesystem without flock runs unlocked."""
    try:
        fh = open(os.path.join(idx, LOCK), "a+")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        fh.close()


def _load_stamps(idx: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(os.path.join(idx, STAMPS)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stamps(idx: str, kept: Dict[str, Dict[str, Any]]) -> None:
    p = os.path.join(idx, STAMPS)
    try:
        with open(p + f".{os.getpid()}.tmp", "w") as fh:
            json.dump(kept, fh)
        os.replace(p + f".{os.getpid()}.tmp", p)
    except OSError:
        pass                                 # read-only: every call checks with the indexer instead


def _build(files: List[str], idx: str, force: bool) -> Tuple[List[str], List[str]]:
    """Index files on all cores: (indexed, failed). A file that cannot be indexed (being created, a member half written) fails alone."""
    if not files:
        return [], []
    try:
        ix = _tv().Indexer(files=list(files), index_dir=idx, parallelism=0, force_rebuild=force)
        ix.build() if force else ix.ensure_indexed()      # ensure_indexed() keeps an index it thinks fresh, even with force_rebuild
        return list(files), []
    except Exception:  # noqa: BLE001
        if len(files) == 1:
            return [], list(files)
        done: List[str] = []
        failed: List[str] = []
        for f in files:
            d, x = _build([f], idx, force)
            done += d
            failed += x
        return done, failed


def _build_waves(files: List[str], idx: str, force: bool, cur: Dict[str, Tuple[int, int]]) -> Tuple[List[str], List[str]]:
    """_build in waves of at most one file per core, largest first, so the progress shows files done (a file is indexed by one worker, so a
    wave of a core's worth loses no parallelism); one wave when they all fit."""
    if not files:
        return [], []
    files = sorted(files, key=lambda f: -cur[f][0])
    n = max(1, int(os.environ.get("MOIRAI_TV_INDEX_WAVE", "0")) or (os.cpu_count() or 8))
    total = sum(cur[f][0] for f in files)
    done: List[str] = []
    failed: List[str] = []
    with RP.step("rebuild index" if force else "index", total=len(files), nbytes=total,
                 detail=f"{len(files)} file(s), {total / 1e6:.0f} MB, {-(-len(files) // n)} wave(s)") as s:
        for i in range(0, len(files), n):
            wave = files[i:i + n]
            s.note(f"wave {i // n + 1}: {len(wave)} file(s), {sum(cur[f][0] for f in wave) / 1e6:.0f} MB, e.g. {os.path.basename(wave[0])}")
            d, x = _build(wave, idx, force)
            done += d
            failed += x
            s.advance(len(wave), nbytes=sum(cur[f][0] for f in wave))
    return done, failed


def stale(files: List[str], idx: str = "") -> List[str]:
    """The files whose index is missing or older than the file (what ensure_index would rebuild), from the stamps alone."""
    idx = idx or index_dir(files)
    kept = _load_stamps(idx) if os.path.isdir(os.path.join(idx, ".dftindex")) else {}
    out = []
    for f in files:
        st, k = _stamp(f), kept.get(f) or {}
        if st and st[0] > 0 and (tuple(k.get("stamp") or ()) != st or k.get("young")):
            out.append(f)
    return out


def ensure_index(files: List[str], idx: str = "", force: bool = False) -> str:
    """Index the files before they are read, so every query reads an index that matches the trace: what is missing or changed since it was
    indexed (a live run still writing) is (re)built on all cores, the rest is left alone. With the stamps of the last build this is one stat per
    file when nothing changed; the indexer is not even opened. A file indexed within YOUNG_S of its last write is rebuilt once more on the next
    call, unchanged stamp or not: an mtime is only as fine as the filesystem's clock (a second on Lustre), so a member written into a reserved
    hole in that tick changes neither size nor mtime. force: rebuild these files' index regardless. A file that cannot be indexed yet is left for
    the next call. Serialized per index (``_index_lock``). Returns the index directory."""
    idx = idx or index_dir(files)
    if not files:
        return idx
    with RP.step("wait for the index", detail="another reader may be indexing these traces"):
        lock = _index_lock(idx)
        lock.__enter__()
    try:
        with RP.step("check index", total=len(files)) as s:
            kept = _load_stamps(idx) if os.path.isdir(os.path.join(idx, ".dftindex")) else {}
            cur = {f: st for f in files if (st := _stamp(f)) and st[0] > 0}    # an empty file is being created: nothing to index yet
            rebuild, update = [], []
            for f, st in cur.items():
                k = kept.get(f) or {}
                if force or (k.get("young") and tuple(k.get("stamp") or ()) == st):
                    rebuild.append(f)
                elif tuple(k.get("stamp") or ()) != st:
                    update.append(f)
            s.advance(len(files), f"{len(rebuild) + len(update)} of {len(files)} file(s) to (re)index")
        if not rebuild and not update:
            return idx
        again = [f for f in update if f in kept]      # indexed before and changed since: the indexer's own check can miss an in-place write
        if again:
            with RP.step("ask the indexer what changed", total=len(again)):
                try:
                    sees = set(_tv().Indexer(files=again, index_dir=idx, parallelism=0).resolve().needs_work)
                except Exception:  # noqa: BLE001
                    sees = set()
            missed = [f for f in again if f not in sees]
            rebuild += missed
            update = [f for f in update if f not in missed]
        now = time.time()
        d1, x1 = _build_waves(update, idx, False, cur)
        d2, x2 = _build_waves(rebuild, idx, True, cur)
        for f in d1 + d2:
            kept[f] = {"stamp": list(cur[f]), "young": now - cur[f][1] / 1e9 < YOUNG_S, "at": round(now, 3)}
        for f in x1 + x2:
            kept.pop(f, None)
        _save_stamps(idx, kept)
        if x1 or x2:
            print(f"trace index: {len(x1 + x2)} file(s) not indexable yet, retried on the next read: {[os.path.basename(f) for f in x1 + x2][:5]}",
                  file=sys.stderr, flush=True)
    finally:
        lock.__exit__(None, None, None)
    return idx


# ------------------------------------------------------------------ live runs: keep the index current between queries
KEEP_EVERY_S = float(os.environ.get("MOIRAI_TV_KEEP_EVERY_S", "3"))     # how often a live run's traces are checked for changes
KEEP_IDLE_S = float(os.environ.get("MOIRAI_TV_KEEP_IDLE_S", "600"))     # the keeper stops once nobody has asked for this long


class IndexKeeper(threading.Thread):
    """Re-indexes a live run's traces as they change, in the background, so a query finds the index already built instead of building it
    (minutes on a multi-GB run). Each round is ensure_index: one stat per file when nothing changed. It stops after KEEP_IDLE_S without a
    touch(); keep_indexed() starts or touches the one keeper per set of traces."""

    def __init__(self, traces: str, every: float = KEEP_EVERY_S, idle: float = KEEP_IDLE_S, pipeline: str = "") -> None:
        super().__init__(daemon=True, name="trace-index-keeper")
        self.traces, self.every, self.idle, self.pipeline = traces, every, idle, pipeline
        self.touched = time.time()
        self.rounds = 0
        self.error = ""

    def touch(self) -> None:
        self.touched = time.time()

    def run(self) -> None:
        try:
            while time.time() - self.touched < self.idle:
                try:
                    with RP.op(self.pipeline, "index", self.traces):      # shown and logged only when it (re)indexed something
                        files = find(self.traces)
                        if files:
                            ensure_index(files)
                    self.rounds += 1
                    self.error = ""
                except Exception as e:  # noqa: BLE001  a bad round must not end the keeper
                    self.error = f"{type(e).__name__}: {e}"
                time.sleep(self.every)
        finally:
            with _KEEP_LOCK:
                if _KEEPERS.get(self.traces) is self:
                    _KEEPERS.pop(self.traces, None)


_KEEPERS: Dict[str, IndexKeeper] = {}
_KEEP_LOCK = threading.Lock()


def keep_indexed(traces: str, pipeline: str = "") -> IndexKeeper:
    """Keep the index of these traces current while a live run writes them (see IndexKeeper): call it whenever a live run is loaded or polled
    for queries. Returns its keeper."""
    with _KEEP_LOCK:
        k = _KEEPERS.get(traces)
        if k is not None and k.is_alive():
            k.touch()
            return k
        k = _KEEPERS[traces] = IndexKeeper(traces, pipeline=pipeline)
        k.start()
        return k


def _rec(row: Dict[str, Any]) -> Dict[str, Any]:
    """A TraceViewer row (flat ``args.x`` columns) as the record it was written as: {name, cat, ph, pid, ts, dur, args}, args.relations as lists."""
    out: Dict[str, Any] = {}
    args: Dict[str, Any] = {}
    rels: Dict[str, List[Tuple[int, Any]]] = {}
    for k, v in row.items():
        if v is None or v == "":
            continue
        if k.startswith("args.relations."):
            kind, _, i = k[len("args.relations."):].partition(".")
            rels.setdefault(kind, []).append((int(i) if i.isdigit() else 0, v))
        elif k.startswith("args."):
            args[k[5:]] = v
        else:
            out[k] = v
    if rels:
        args["relations"] = {kind: [v for _, v in sorted(xs)] for kind, xs in rels.items()}
    out["args"] = args
    return out


def read_file(path: str, idx: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """One indexed trace file through TraceViewer: its provenance records, and the number of events of each category."""
    U = _tv()
    base = U.TraceViewer(path, idx)
    cols = set(base.columns)                 # from the index: asking a phase()d view infers the schema by scanning the file
    tv = base.phase("any")
    recs = [_rec(r) for r in tv.filter(U.col("name").is_in(list(PROV_NAMES))).collect().to_dicts()]     # metadata columns are not in the index schema: no select
    acols = [c for c in ("name", "cat", "ph", "pid", "ts", "dur") if c in cols] + sorted(c for c in cols if c.startswith("args.relations."))
    recs += [_rec(r) for r in tv.filter(U.col("cat").is_in(list(ACTIVITY_CATS))).select(acols).collect().to_dicts()]
    cats: Dict[str, int] = {}
    for r in tv.filter(U.col("ph") != 4).group_by("cat").agg(U.col("name").count()).collect().to_dicts():     # ph 4: counter events, not counted
        n = next((v for k, v in r.items() if k != "cat"), 0)
        cats[str(r.get("cat"))] = int(n or 0)
    return recs, cats


def row_counts(traces: str) -> Dict[str, int]:
    """Row counts of the traces by TraceViewer group-by only, no row is turned into a Python record: {"<cat>:<name>": rows} for the provenance
    names (ET/EH/ER) and the activity categories, plus "files". Seconds where ``read(...).counts()`` takes minutes on a multi-GB run; two sets of
    traces that hold the same records (a byte-for-byte published copy) give the same table."""
    files = find(traces)
    out: Counter = Counter()
    if not files:
        return {"files": 0}
    idx = ensure_index(files)
    U = _tv()
    with RP.step("count provenance rows", total=len(files)) as s:
        for f in files:
            tv = U.TraceViewer(f, idx).phase("any")
            sel = U.col("name").is_in(list(PROV_NAMES)) | U.col("cat").is_in(list(ACTIVITY_CATS))
            for r in tv.filter(sel).group_by("cat", "name").agg(U.col("ts").count()).collect().to_dicts():
                n = next((v for k, v in r.items() if k not in ("cat", "name")), 0)
                out[f"{r.get('cat')}:{r.get('name')}"] += int(n or 0)
            s.advance(1, os.path.basename(f), (_stamp(f) or (0, 0))[0])
    return {**out, "files": len(files)}


def _stamp(f: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(f)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class Tail:
    """Incremental reader of every trace file under ``traces``, through TraceViewer: each poll indexes and reads the files that are new or
    changed since the last one (a run still writing), so the graph a caller builds from the records stays current. A file that cannot be
    read yet (being created) is retried on the next poll."""

    def __init__(self, traces: str, workers: int = 0) -> None:
        self.traces = traces
        self.seen: Dict[str, Tuple[int, int]] = {}
        self.stalled = 0                     # polls where a changed file could not be read
        self.workers = workers
        self.files: Dict[str, Tuple[Dict[str, int], int]] = {}   # per file: (other events by category, records) -> Graph.set_files
        self._keys: set = set()              # records already returned: a changed file is read whole again, only what is new is returned
        self.young: set = set()              # files modified within YOUNG_S of their last read: read again (and re-indexed) until they settle

    def poll(self) -> List[Dict[str, Any]]:
        files = find(self.traces)
        changed = {f: st for f in files if (st := _stamp(f)) and st[0] > 0 and st != self.seen.get(f)}
        if not changed:
            return []
        idx = index_dir(files)
        try:
            ensure_index(list(changed), idx)     # rebuilds what changed since it was indexed, and once more what was indexed while young
        except Exception:  # noqa: BLE001  a file being created: try again on the next poll
            self.stalled += 1
            return []
        out: List[Dict[str, Any]] = []
        for f, got in _read_many(list(changed), idx, self.workers):
            if got is None:
                self.stalled += 1
                continue
            recs, cats = got
            if time.time() - changed[f][1] / 1e9 < YOUNG_S:
                self.young.add(f)                # an mtime is only as fine as the kernel clock tick: a write in the same tick would look unchanged
            else:
                self.young.discard(f)
                self.seen[f] = changed[f]
            self.files[f] = file_stats(cats, recs)
            for r in recs:
                k = _key(r)
                if k not in self._keys:
                    self._keys.add(k)
                    out.append(r)
        return out


YOUNG_S = 2.0


def _key(r: Dict[str, Any]) -> tuple:
    a = r.get("args") or {}
    return (r.get("name"), r.get("pid"), r.get("ts"), a.get("id"), a.get("type"), a.get("relation"), a.get("subject"), a.get("object"))


def file_stats(cats: Dict[str, int], recs: List[Dict[str, Any]]) -> Tuple[Dict[str, int], int]:
    """(events by category other than the provenance records, every record) of one file, from its TraceViewer category counts."""
    prov = sum(1 for r in recs if r.get("name") in PROV_NAMES)
    other = {k: v for k, v in cats.items() if k not in ACTIVITY_CATS}
    if other.get("dftracer"):
        other["dftracer"] = max(0, other["dftracer"] - prov)
    return {k: v for k, v in other.items() if v}, sum(cats.values())


def _read_one(a: Tuple[str, str]):
    try:
        return a[0], read_file(a[0], a[1])
    except Exception:  # noqa: BLE001
        return a[0], None


def _read_many(files: List[str], idx: str, workers: int = 0):
    """read_file over many files, as the read's "read records" step: one result per file as it completes."""
    nbytes = {f: (_stamp(f) or (0, 0))[0] for f in files}
    with RP.step("read records", total=len(files), nbytes=sum(nbytes.values()), detail=f"{len(files)} file(s) through TraceViewer") as s:
        for f, res in _read_many_raw(files, idx, workers):
            s.advance(1, os.path.basename(f) + ("" if res is not None else ": not readable yet"), nbytes.get(f, 0))
            yield f, res


def _read_many_raw(files: List[str], idx: str, workers: int = 0):
    """read_file over many files: one process per file (all cores) when there are several, as TraceViewer's rows become Python records."""
    # capped (MOIRAI_READ_WORKERS, default 16): one process per core on a 100+ file, multi-GB run was killed (BrokenProcessPool)
    n = workers or min(len(files), os.cpu_count() or 4, int(os.environ.get("MOIRAI_READ_WORKERS", "16")))
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool
    todo = list(files)
    while todo:
        if len(todo) < 2 or n < 2:
            for f in todo:
                yield _read_one((f, idx))
            return
        # spawn, not fork: the caller already runs TraceViewer's native threads (the index check), and a forked child can hang on their locks
        try:
            with ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn")) as ex:
                for f, res in zip(list(todo), ex.map(_read_one, [(f, idx) for f in todo])):
                    todo.remove(f)
                    yield res
        except BrokenProcessPool:        # a worker was killed (memory): the files not yet read again with half the workers, down to one process
            n //= 2


class Graph:
    """The provenance graph of a set of records, built incrementally."""

    def __init__(self) -> None:
        self.types: Dict[str, Dict[str, Any]] = {}
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.activities: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, str]] = []            # activity relations: {activity, kind, entity}
        self.relations: List[Dict[str, str]] = []        # entity relations: {relation, subject, object}
        self._events = Counter()                         # every other event, by category (records added one by one)
        self._total = 0
        self._files: Dict[str, Tuple[Dict[str, int], int]] = {}   # per trace file read with TraceViewer: (other events by category, records)
        self._seen = set()

    @property
    def events(self) -> Counter:
        """Events other than provenance records, by category: from the files' TraceViewer counts when it was read with Tail/read."""
        if not self._files:
            return Counter(self._events)
        c: Counter = Counter()
        for cats, _ in self._files.values():
            c.update(cats)
        return c

    @property
    def total(self) -> int:
        return sum(n for _, n in self._files.values()) if self._files else self._total

    def set_files(self, files: Dict[str, Tuple[Dict[str, int], int]]) -> None:
        """The per-file event counts a Tail keeps (Tail.files): they replace counting records one by one."""
        self._files.update(files)

    def add(self, r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Take one record; returns what it added to the graph (what a live view shows), or None."""
        self._total += 1
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
            self._events[cat] += 1
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


def _read_serial(traces: str) -> Graph:
    """The graph of the traces under ``traces`` (one file or several), read through TraceViewer."""
    g, t = Graph(), Tail(traces, workers=1)
    while True:
        recs = t.poll()
        g.set_files(t.files)
        if not recs:
            return g
        g.extend(recs)


def _merge(g: Graph, o: Graph) -> None:
    """Add what another file's graph holds, with the dedup the serial reader applies (entity ids, activity ids, relation keys)."""
    g._total += o._total
    g._files.update(o._files)
    for k, v in o.types.items():
        g.types.setdefault(k, v)
    for k, v in o.entities.items():
        g.entities.setdefault(k, v)
    g._events.update(o._events)
    known = {(r["relation"], r["subject"], r["object"]) for r in g.relations}
    for r in o.relations:
        k = (r["relation"], r["subject"], r["object"])
        if k not in known:
            known.add(k)
            g.relations.append(r)
    by_act: Dict[str, List[Dict[str, str]]] = {}
    for e in o.edges:
        by_act.setdefault(e["activity"], []).append(e)
    taken = {a["id"] for a in g.activities}
    for a in o.activities:
        if a["id"] in taken:
            continue
        taken.add(a["id"])
        g.activities.append(a)
        g.edges.extend(by_act.get(a["id"], []))


def read(traces: str, workers: int = 0) -> Graph:
    """The whole graph of the traces, through TraceViewer: every file is indexed on all cores, then read one file per process and merged
    (the same dedup as one reader)."""
    files = find(traces)
    if not files:
        return Graph()
    idx = ensure_index(files)
    g = Graph()
    for f, got in _read_many(files, idx, workers):       # files arrive as their workers finish them; the graph dedups as one reader does
        if got is None:
            continue
        recs, cats = got
        g.extend(recs)
        g.set_files({f: file_stats(cats, recs)})
    return g
