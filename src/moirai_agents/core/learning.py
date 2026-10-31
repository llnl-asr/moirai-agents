"""Lessons: what the user asked to remember, and what agents learned. Each belongs to the capability it concerns and
is written into that capability's LESSONS.md, the file its skill tells agents to read. What a person stated applies at
once; what an agent proposes waits for approval in the Lessons panel. One copy of each fact: a near-duplicate is refused."""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import capstore


def _file() -> Path:
    d = Path(os.environ.get("MOIRAI_AGENTS_LEARNING_DIR", "~/.moirai-agents/learning")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d / "lessons.jsonl"


def all_lessons() -> Dict[str, dict]:
    cur: Dict[str, dict] = {}
    f = _file()
    for line in f.read_text().splitlines() if f.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        cur[e["id"]] = {**cur.get(e["id"], {}), **e}
    return cur


def _append(rec: dict) -> None:
    with open(_file(), "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _write(cap: str, text: str, lid: str) -> None:
    f = capstore.shadow(cap) / "LESSONS.md"
    with open(f, "a") as fh:
        fh.write(f"\n- [{lid}] {text.strip()}\n")


def add(capability: str, text: str, source: str = "agent", evidence: str = "", stage: str = "", pipeline: str = "") -> dict:
    """source='user': stated by a person, applied now. source='agent': pending until a person approves it."""
    if not (text or "").strip():
        return {"status": "error", "error": "a lesson needs its text"}
    if capability not in capstore.all_caps():
        return {"status": "error", "error": f"unknown capability {capability}"}
    if source == "agent" and not evidence.strip():
        return {"status": "error", "error": "no source, no lesson: say where this came from (run, log line, file:line)"}
    low = text.lower().strip()
    for o in all_lessons().values():
        if o.get("status") in ("applied", "pending") and o.get("capability") == capability and \
                difflib.SequenceMatcher(None, low, o["text"].lower()).ratio() > 0.88:
            return {"status": "duplicate", "id": o["id"], "text": o["text"], "note": "already known: keep one copy of each fact"}
    lid = "L-" + uuid.uuid4().hex[:8]
    rec = {"id": lid, "ts": round(time.time(), 1), "capability": capability, "text": text.strip(), "source": source, "evidence": evidence,
           "stage": stage, "pipeline": pipeline, "user": os.environ.get("MOIRAI_AGENTS_USER", ""),
           "status": "applied" if source == "user" else "pending"}
    _append(rec)
    if source == "user":
        _write(capability, text, lid)
    return {"status": rec["status"], "id": lid, "capability": capability}


def _unwrite(cap: str, lid: str) -> None:
    """Take a lesson out of its capability's LESSONS.md."""
    f = capstore.shadow(cap) / "LESSONS.md"
    f.write_text(re.sub(rf"\n?- \[{re.escape(lid)}\] .*?(?=\n+- \[L-|\Z)", "", f.read_text(), flags=re.S).rstrip() + "\n")


def decide(lid: str, approve: bool, by: str = "") -> dict:
    o = all_lessons().get(lid)
    if not o:
        return {"status": "error", "error": f"unknown lesson {lid}"}
    if o["status"] != "pending":
        return {"status": "error", "error": f"lesson {lid} is already {o['status']}"}
    if approve:
        replaced = o.get("supersedes") or []
        for oid in [replaced] if isinstance(replaced, str) else replaced:
            old = all_lessons().get(oid)
            if old and old["status"] in ("applied", "pending"):
                if old["status"] == "applied":
                    _unwrite(old["capability"], oid)
                _append({"id": oid, "status": "superseded", "superseded_by": lid})
        _write(o["capability"], o["text"], lid)
    _append({"id": lid, "status": "applied" if approve else "rejected", "decided_by": by, "decided": round(time.time(), 1)})
    return {"status": "ok", "id": lid, "applied": approve}


def merge(ids: List[str], text: str, capability: str, note: str, by: str = "") -> dict:
    """One pending lesson to replace several: they stay in force until a person accepts it."""
    rows = all_lessons()
    missing = [i for i in ids if i not in rows]
    if len(ids) < 2 or missing:
        return {"status": "error", "error": f"name at least two known lessons; unknown: {missing}"}
    if capability not in capstore.all_caps():
        return {"status": "error", "error": f"unknown capability {capability}"}
    if not (text or "").strip():
        return {"status": "error", "error": "a lesson needs its text"}
    lid = "L-" + uuid.uuid4().hex[:8]
    _append({"id": lid, "ts": round(time.time(), 1), "capability": capability, "text": text.strip(), "source": "agent", "evidence": "merge of " + ", ".join(ids),
             "status": "pending", "supersedes": list(ids), "revisions": [{"by": by or "agent", "note": note, "ts": round(time.time(), 1)}]})
    return {"status": "ok", "id": lid, "replaces": list(ids)}


def revise(lid: str, text: str, capability: str = "", note: str = "", approve: bool = False, by: str = "") -> dict:
    """Refine a lesson. Pending: edited in place. Rejected: reopened with the edit. Applied: a pending correction that replaces it when accepted,
    or at once with approve. approve is a person's decision: only the panel passes it."""
    o = all_lessons().get(lid)
    if not o:
        return {"status": "error", "error": f"unknown lesson {lid}"}
    if o["status"] == "superseded":
        return {"status": "error", "error": f"lesson {lid} was replaced by {o.get('superseded_by')}: refine that one"}
    text, capability = (text or o["text"]).strip(), capability.strip() or o["capability"]
    if capability not in capstore.all_caps():
        return {"status": "error", "error": f"unknown capability {capability}"}
    if text == o["text"] and capability == o["capability"] and not approve:
        return {"status": "error", "error": "nothing changed"}
    revisions = o.get("revisions", []) + [{"by": by or "agent", "note": note, "ts": round(time.time(), 1)}]
    if o["status"] == "applied":
        new = "L-" + uuid.uuid4().hex[:8]
        _append({**{k: v for k, v in o.items() if k not in ("decided_by", "decided")}, "id": new, "ts": round(time.time(), 1), "capability": capability, "text": text, "status": "pending",
                 "supersedes": lid, "revisions": revisions})
        lid = new
    else:
        _append({"id": lid, "text": text, "capability": capability, "status": "pending", "revisions": revisions})
    return decide(lid, True, by) if approve else {"status": "ok", "id": lid, "applied": False}


def listing(status: Optional[str] = None, limit: int = 200) -> List[dict]:
    rows = sorted(all_lessons().values(), key=lambda r: -r.get("ts", 0))
    return [r for r in rows if not status or r.get("status") == status][:limit]


def search(query: str, k: int = 8) -> List[dict]:
    """Rank capability skills and lessons by word overlap with the query: locate, do not read everything."""
    words = set(re.findall(r"[a-z0-9_]{3,}", query.lower()))
    out = []
    for n, (_, d, m) in capstore.all_caps().items():
        hay = " ".join([n, m.get("description", ""), " ".join(m.get("tools", [])), " ".join(m.get("stages", []))]).lower()
        score = sum(1 for w in words if w in hay)
        if score:
            out.append({"kind": "capability", "name": n, "score": score, "description": m.get("description", "")[:160], "tools": m.get("tools", [])})
    for r in all_lessons().values():
        if r.get("status") == "applied":
            score = sum(1 for w in words if w in r["text"].lower())
            if score:
                out.append({"kind": "lesson", "id": r["id"], "capability": r["capability"], "score": score, "text": r["text"][:300]})
    return sorted(out, key=lambda x: -x["score"])[:k]


def context(capabilities: List[str], limit: int = 40) -> str:
    """Approved lessons of these capabilities, for a turn's system prompt (read fresh each turn)."""
    rows = [r for r in listing("applied") if not capabilities or r["capability"] in capabilities][:limit]
    if not rows:
        return ""
    return "\n\nLessons already learned (follow them):\n" + "\n".join(f"- ({r['capability']}) {r['text']}" for r in rows)
