"""Do lessons or capabilities overlap or contradict each other? A deterministic look at the store: it finds what a person or an agent should consolidate, and changes nothing.

A group is {kind: conflict|overlap|crowded, ids, ...}. A group someone decided is fine is kept in <learning>/consolidation_keep.json and not reported again.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

from . import capstore, learning

OVERLAP = 0.55            # shared words, of all the words of the smaller text
CONFLICT = 0.4            # about the same thing, but one forbids what the other allows
RULE_OVERLAP = 0.7
CAP_OVERLAP = 0.5
MIN_WORDS = 5
MIN_SHARED_RULES = 2      # one shared line is a coincidence
CROWDED = 25              # lessons on one capability: too many to read before acting
SHOWN_CHARS = 200
STOP = frozenset("the and for with from that this not was were are but has have into when then than they them its use used using must should can will all any each one only".split())
FORBIDS = re.compile(r"\b(?:never|not|avoid|don't|cannot|must not)\s+([a-z0-9_]{3,})", re.I)
BOILERPLATE = ("becomes a capability", "follow `capability_builder`", "A failed verdict comes here")


def _words(text: str) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower()) if w not in STOP}


def _alike(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / max(min(len(a), len(b)), 1)


def _forbidden(text: str) -> Set[str]:
    """The word each negative applies to: `never build X` forbids build."""
    return {m.group(1).lower() for m in FORBIDS.finditer(text)} - STOP


def _opposed(x: str, y: str) -> bool:
    """One forbids what the other does."""
    fx, fy = _forbidden(x), _forbidden(y)
    return bool((fx - fy) & _words(y)) or bool((fy - fx) & _words(x))


def _kept() -> Set[str]:
    f = capstore._learning_dir() / "consolidation_keep.json"
    try:
        return set(json.loads(f.read_text()))
    except (OSError, ValueError):
        return set()


def _key(ids: Iterable[str]) -> str:
    return "|".join(sorted(ids))


def keep(ids: List[str], why: str) -> Dict[str, Any]:
    """Record that these are different on purpose, so they are not reported again."""
    if len(ids) < 2 or not (why or "").strip():
        return {"status": "error", "error": "name at least two ids and say why they stay apart"}
    f = capstore._learning_dir() / "consolidation_keep.json"
    f.write_text(json.dumps(sorted(_kept() | {_key(ids)}), indent=1))
    return {"status": "ok", "kept": _key(ids), "why": why}


def _groups(pairs: List[Tuple[str, str]]) -> List[List[str]]:
    """Connected ids: a overlaps b and b overlaps c is one thing to consolidate."""
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    out: Dict[str, List[str]] = {}
    for x in parent:
        out.setdefault(find(x), []).append(x)
    return [sorted(g) for g in out.values()]


def lessons() -> List[Dict[str, Any]]:
    rows = [r for r in learning.all_lessons().values() if r.get("status") in ("applied", "pending")]
    words = {r["id"]: _words(r["text"]) for r in rows}
    near, opposed = [], []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            wa, wb = words[a["id"]], words[b["id"]]
            if min(len(wa), len(wb)) < MIN_WORDS:
                continue
            like = _alike(wa, wb)
            if like >= CONFLICT and _opposed(a["text"], b["text"]):
                opposed.append((a["id"], b["id"]))
            elif like >= OVERLAP:
                near.append((a["id"], b["id"]))
    by_id = {r["id"]: r for r in rows}
    clash = {i for p in opposed for i in p}
    out = []
    for g in _groups(near + opposed):
        out.append({"kind": "conflict" if clash & set(g) else "overlap", "ids": g, "capabilities": sorted({by_id[i]["capability"] for i in g}),
                    "texts": [by_id[i]["text"][:SHOWN_CHARS] for i in g]})
    counts: Dict[str, List[str]] = {}
    for r in rows:
        counts.setdefault(r["capability"], []).append(r["id"])
    out += [{"kind": "crowded", "ids": sorted(ids), "capabilities": [c], "texts": [f"{len(ids)} lessons on {c}"]} for c, ids in counts.items() if len(ids) > CROWDED]
    return out


def _rules(skill: str) -> List[str]:
    lines = [x[2:].strip() for x in skill.splitlines() if x.startswith("- ")]
    return [x for x in lines if not any(b in x for b in BOILERPLATE)]


def capabilities() -> List[Dict[str, Any]]:
    caps = []
    for name, (_, d, meta) in sorted(capstore.all_caps().items()):
        try:
            caps.append((name, meta.get("description", ""), _rules((d / "SKILL.md").read_text(errors="replace"))))
        except OSError:
            continue
    out = []
    for i, (a, da, ra) in enumerate(caps):
        for b, db, rb in caps[i + 1:]:
            same, clash = [], []
            for x in ra:
                wx = _words(x)
                if len(wx) < MIN_WORDS:
                    continue
                for y in rb:
                    wy = _words(y)
                    if len(wy) < MIN_WORDS:
                        continue
                    like = _alike(wx, wy)
                    if like >= RULE_OVERLAP and _opposed(x, y):
                        clash.append(f"{a}: {x[:SHOWN_CHARS]} | {b}: {y[:SHOWN_CHARS]}")
                    elif like >= RULE_OVERLAP:
                        same.append(x[:SHOWN_CHARS])
            wa, wb = _words(da.split(". ")[0]), _words(db.split(". ")[0])      # what follows the first sentence says when to load it, in the same words everywhere
            subject = min(len(wa), len(wb)) >= MIN_WORDS and _alike(wa, wb) >= CAP_OVERLAP
            if clash:
                out.append({"kind": "conflict", "ids": [a, b], "texts": clash[:3]})
            elif len(same) >= MIN_SHARED_RULES or subject:
                out.append({"kind": "overlap", "ids": [a, b], "texts": same[:3] or [da[:SHOWN_CHARS], db[:SHOWN_CHARS]]})
    return out


def report() -> Dict[str, Any]:
    """What needs consolidating now."""
    kept = _kept()
    fresh = lambda groups: [g for g in groups if _key(g["ids"]) not in kept]  # noqa: E731
    return {"lessons": fresh(lessons()), "capabilities": fresh(capabilities()), "checked": round(time.time(), 1)}
