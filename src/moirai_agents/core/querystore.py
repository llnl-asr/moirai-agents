"""The queries asked about a trace, kept with the trace so everyone who can read it sees what was asked and what came back.

Layout (one root, shared by every user; default /p/vast1/moirai/provenance-queries, MOIRAI_AGENTS_QUERY_STORE moves it):

    <root>/<trace key>/trace.json                                   what traces these are (written once)
    <root>/<trace key>/queries/<user>/<asked at, UTC>-<answer id>.json   one file per query

<trace key> is <app>/<timestamp> for a published run (/.../provenance-traces/<app>/<timestamp>/raw), else local/<16 hex of its real path>.
Each query is its own file under the asker's folder, named by when it was asked: two people asking at once never write the same file,
and only the asker's process rewrites it (when the write-up or the explanation is added). Files are written whole (temp + rename).

A query file is self-contained and does not depend on how the UI shows it (schema moirai.query/1):
    question, asked_by, asked_at, category, pipeline, trace, the answer (text, the LLM's write-up and its check), result, plan, steps
    with their cited records, the TraceViewer queries, files read, gaps, evidence, confidence, the explanation (four parts, their checks
    against the traces, the confidence score), and `record`: the answer record exactly as the engine wrote it, so nothing is ever lost.
"""
from __future__ import annotations

import getpass
import grp
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "moirai.query/1"
SHARED_ROOT = "/p/vast1/moirai/provenance-queries"
PUBLISHED = re.compile(r"/provenance-traces/(?P<app>[A-Za-z0-9._-]+)/(?P<ts>\d{8}_\d{6})(?:/|$)")
_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {}


def root() -> Path:
    if os.environ.get("MOIRAI_AGENTS_QUERY_STORE"):
        return Path(os.environ["MOIRAI_AGENTS_QUERY_STORE"]).expanduser()
    shared = Path(SHARED_ROOT)
    if os.access(shared, os.W_OK) or os.access(shared.parent, os.W_OK):
        return shared
    from ..env import state_dir
    return state_dir("provenance-queries")


def who() -> str:
    u = os.environ.get("MOIRAI_AGENTS_USER") or ""
    if not u:
        try:
            u = getpass.getuser()
        except Exception:  # noqa: BLE001
            u = "unknown"
    return safe_user(u)


def safe_user(u: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", u or "")[:64] or "unknown"


def clean(rec: Dict[str, Any]) -> Dict[str, Any]:
    """An answer record as the engine wrote it: without what listing derives (category, stored_file, gaps read from the write-up)."""
    r = {k: v for k, v in rec.items() if k not in ("category", "stored_file")}
    if isinstance(r.get("gaps"), list):
        r["gaps"] = [g for g in r["gaps"] if not (isinstance(g, dict) and g.get("kind") == "reported")]
    return r


def explanation_tag(rec: Dict[str, Any]) -> Dict[str, Any]:
    """What an explanation was computed for: a new result or write-up makes it stale."""
    return {"result_hash": rec.get("result_hash", ""), "synthesis": len(rec.get("synthesis") or ""), "answer": len(rec.get("answer") or "")}


def trace_dir(traces: str) -> str:
    """The directory of a pipeline's traces (a dir, a glob, a file, or several joined by os.pathsep)."""
    parts = [p for p in str(traces or "").split(os.pathsep) if p]
    dirs = []
    for p in parts:
        p = os.path.realpath(os.path.expanduser(p))
        dirs.append(p if os.path.isdir(p) else os.path.dirname(p.split("*")[0].rstrip("/")) or p)
    return os.path.commonpath(dirs) if dirs else ""


def trace_key(traces: str) -> str:
    d = trace_dir(traces)
    if not d:
        return ""
    m = PUBLISHED.search(d + "/")
    if m:
        return f"{m.group('app')}/{m.group('ts')}"
    return "local/" + hashlib.sha1(d.encode()).hexdigest()[:16]


def category(rec: Dict[str, Any]) -> str:
    """A deterministic category from the plan's operations; an agent-written answer (no plan steps) is its own."""
    steps = (rec.get("plan") or {}).get("steps") or []
    if not steps:
        return "Agent answers"
    from . import aspects
    return aspects.label_of_plan(steps)          # Traceability, Accountability, Decision making or Performance


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


DIR_MODE, FILE_MODE = 0o2770, 0o660         # group members read every query and add their own folder; no access for others


def group_id() -> Optional[int]:
    """The group every query artifact belongs to: MOIRAI_AGENTS_QUERY_GROUP (default moirai), else the group of the store's parent folder."""
    try:
        return grp.getgrnam(os.environ.get("MOIRAI_AGENTS_QUERY_GROUP", "moirai")).gr_gid
    except KeyError:
        try:
            return root().parent.stat().st_gid
        except OSError:
            return None


def share(p: Path) -> None:
    """Give one store path the shared group and mode (setgid g=u for folders, rw for the group on files, nothing for others). A path
    another user owns is left as it is: only its owner can change it, and they did when they made it."""
    try:
        st = p.stat()
    except OSError:
        return
    gid = group_id()
    try:
        if gid is not None and st.st_gid != gid:
            os.chown(p, -1, gid)
        os.chmod(p, DIR_MODE if p.is_dir() else FILE_MODE)
    except PermissionError:
        pass


def _write(path: Path, doc: Dict[str, Any]) -> None:
    """Whole-file write (temp + rename), with the shared group and mode set before it appears."""
    _mkdirs(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1, default=str)
        share(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mkdirs(d: Path) -> None:
    """Every new level group-shared (setgid, g=u, no other): the root's group (moirai) follows down."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        p = p.parent
    for x in reversed(missing):
        try:
            x.mkdir(mode=DIR_MODE)
        except FileExistsError:
            pass
        share(x)


def fix_permissions(key: str = "") -> Dict[str, Any]:
    """Re-apply the shared group and mode to everything this user owns in the store (one trace key, or all). Returns {fixed, others}:
    others are paths owned by someone else that are not group-shared (only their owner can fix them)."""
    base = root() / key if key else root()
    me, gid, fixed, others = os.getuid(), group_id(), 0, []
    for p in [base, *base.rglob("*")] if base.exists() else []:
        try:
            st = p.lstat()
        except OSError:
            continue
        want = DIR_MODE if p.is_dir() else FILE_MODE
        ok = (gid is None or st.st_gid == gid) and (st.st_mode & 0o7777) == want
        if ok:
            continue
        if st.st_uid == me:
            share(p)
            fixed += 1
        else:
            others.append(str(p))
    return {"fixed": fixed, "others": others[:50]}


def _pipeline_of(pid: str) -> Dict[str, Any]:
    from . import pipeline as P
    try:
        c = P.load(pid)
    except Exception:  # noqa: BLE001
        return {"id": pid}
    return {"id": pid, "name": c.get("name", ""), "kind": c.get("kind", ""), "parent": c.get("parent", "")}


def _traces_of(pid: str) -> str:
    from . import answers as A
    from . import pipeline as P
    try:
        return A.traces_of(P.load(pid))
    except Exception:  # noqa: BLE001
        return ""


def path_of(key: str, user: str, rec: Dict[str, Any]) -> Path:
    ts = float(rec.get("ts") or time.time())
    return root() / key / "queries" / user / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(ts))}-{rec.get('id', 'noid')}.json"


def document(pid: str, rec: Dict[str, Any], key: str, user: str, explanation: Optional[Dict[str, Any]] = None,
             old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The stored form of one query: normalized fields for any reader, and the engine's record verbatim."""
    ts = float(rec.get("ts") or time.time())
    expl = explanation if explanation is not None else (old or {}).get("explanation")
    steps = rec.get("steps") or []
    return {
        "schema": SCHEMA, "id": rec.get("id"), "question": rec.get("question", ""), "asked_by": user, "asked_at": _iso(ts), "asked_ts": ts,
        "category": category(rec), "pipeline": _pipeline_of(pid), "chat": rec.get("chat", ""),
        "trace": {"key": key, "dir": trace_dir(_traces_of(pid)), "files": rec.get("reads") or []},
        "engine": rec.get("engine") or "agent", "engine_info": rec.get("engine_info") or {},
        "answer": {"text": rec.get("answer") or rec.get("synthesis") or (rec.get("result") or {}).get("headline", ""),
                   "llm_response": rec.get("synthesis") or (rec.get("answer") if rec.get("engine") != "traceviewer" else ""),
                   "check": rec.get("synthesis_check") or {}, "verified": rec.get("verified"), "problems": rec.get("problems") or []},
        "result": rec.get("result") or {}, "result_hash": rec.get("result_hash", ""),
        "plan": rec.get("plan") or {}, "steps": steps,
        "citations": [{"step": s.get("step"), "records": s.get("cites") or []} for s in steps],
        "queries": rec.get("queries") or [], "gaps": rec.get("gaps") or [], "evidence": rec.get("evidence") or [],
        "confidence": (expl or {}).get("confidence") if isinstance((expl or {}).get("confidence"), dict) else rec.get("confidence"),
        "explanation": expl, "lineage": rec.get("lineage"), "stats": rec.get("stats") or {},
        "record": rec,
        "saved_at": _iso(time.time()), "created_at": (old or {}).get("created_at") or _iso(time.time()),
    }


def save(pid: str, rec: Dict[str, Any], explanation: Optional[Dict[str, Any]] = None, user: str = "") -> Optional[str]:
    """Keep (or update) one query with its trace. Returns the file, or None when the pipeline has no traces. Never raises into the caller's
    answer: a store that cannot be written is logged in the returned error file name only."""
    try:
        key = trace_key(_traces_of(pid))
        if not key or not rec.get("id"):
            return None
        user = safe_user(user or rec.get("asked_by") or who())
        rec = clean(rec)
        base = root() / key
        _mkdirs(base / "queries" / user)
        tj = base / "trace.json"
        if not tj.exists():
            try:
                _write(tj, {"schema": SCHEMA, "key": key, "dir": trace_dir(_traces_of(pid)), "first_pipeline": _pipeline_of(pid), "created_at": _iso(time.time())})
            except OSError:
                pass
        p = path_of(key, user, rec)
        old = None
        if p.exists():
            try:
                old = json.loads(p.read_text())
            except ValueError:
                old = None
        with _LOCK:
            _write(p, document(pid, rec, key, user, explanation, old))
        _CACHE.pop(key, None)
        return str(p)
    except Exception as e:  # noqa: BLE001  keeping the answer must not depend on the shared store
        return f"error: {type(e).__name__}: {e}"


def stored(traces: str) -> List[Dict[str, Any]]:
    """Every query kept for these traces, from every user, oldest first. Cached on the folders' modification times."""
    key = trace_key(traces)
    if not key:
        return []
    q = root() / key / "queries"
    try:
        users = sorted(d for d in q.iterdir() if d.is_dir())
    except OSError:
        return []
    sig = tuple((d.name, d.stat().st_mtime_ns) for d in users)
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    docs = []
    for d in users:
        for f in sorted(d.glob("*.json")):
            try:
                doc = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if doc.get("schema") == SCHEMA and isinstance(doc.get("record"), dict):
                doc["_file"] = str(f)
                docs.append(doc)
    docs.sort(key=lambda x: float(x.get("asked_ts") or 0))
    _CACHE[key] = (sig, docs)
    return docs


def explanation(pid: str, aid: str) -> Optional[Dict[str, Any]]:
    """The explanation kept with a query (four parts, checks, confidence), when one was kept."""
    for d in stored(_traces_of(pid)):
        if d.get("id") == aid and (d.get("explanation") or {}).get("parts"):
            return d["explanation"]
    return None
