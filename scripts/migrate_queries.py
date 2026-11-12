#!/usr/bin/env python3
"""Keep every existing query with its trace in the shared query store (core.querystore): the answer record verbatim, the LLM's write-up,
gaps, evidence, citations, and the explanation (four parts, their checks against the traces, the confidence score), computed now.

    .venv/bin/python scripts/migrate_queries.py [--pipeline ID] [--force] [--no-explain]

Idempotent: a query already kept with an explanation for the same result and write-up is skipped (--force recomputes). Who asked: the
answer's asked_by, else the user recorded on the pipeline, else the owner of the pipeline's folder.
"""
import argparse
import json
import os
import pwd
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moirai_agents.core import answers as A  # noqa: E402
from moirai_agents.core import capstore as CS  # noqa: E402
from moirai_agents.core import pipeline as P  # noqa: E402
from moirai_agents.core import querystore as QS  # noqa: E402
from moirai_agents.env import group_umask, load_env  # noqa: E402


def local_answers(pid):
    """The pipeline's own answers (latest copy of each), as the engine wrote them."""
    f = P.pdir(pid) / "answers.jsonl"
    try:
        rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    last = {}
    for r in rows:
        last[r.get("id")] = r
    return [r for r in last.values() if r.get("id")]


def owner(c):
    if c.get("user"):
        return c["user"]
    try:
        return pwd.getpwuid(P.pdir(c["id"]).stat().st_uid).pw_name
    except (KeyError, OSError):
        return QS.who()


def explain(pid, aid, rec):
    r = CS.call("query_explain", "query_explain", pipeline=pid, answer_id=aid)
    r["checks"] = CS.call("query_explain", "query_explain_validate", pipeline=pid, answer_id=aid).get("parts")
    r["confidence"] = CS.call("answer_confidence", "confidence_score", pipeline=pid, answer_id=aid)
    r["for"], r["computed_at"] = QS.explanation_tag(rec), round(time.time(), 1)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-explain", action="store_true")
    a = ap.parse_args()
    load_env()
    group_umask()
    print(f"query store: {QS.root()}")
    tally = {"kept": 0, "skipped": 0, "no_traces": 0, "explain_failed": 0, "errors": 0}
    for c in P.list_all(""):
        if a.pipeline and c["id"] != a.pipeline:
            continue
        recs = local_answers(c["id"])
        if not recs:
            continue
        key = QS.trace_key(A.traces_of(c))
        if not key:
            tally["no_traces"] += len(recs)
            print(f"- {c['id']}: {len(recs)} answers, no traces: not kept")
            continue
        user = owner(c)
        kept = {d["id"]: d for d in QS.stored(A.traces_of(c))}
        print(f"- {c['id']} -> {key} ({len(recs)} answers, asked by {user})", flush=True)
        for rec in recs:
            rec = {**rec, "asked_by": rec.get("asked_by") or user}
            d = kept.get(rec["id"])
            if d and not a.force and (a.no_explain or ((d.get("explanation") or {}).get("parts") and d["explanation"].get("for") == QS.explanation_tag(rec))):
                tally["skipped"] += 1
                continue
            expl = None
            if not a.no_explain:
                try:
                    expl = explain(c["id"], rec["id"], rec)
                    if expl.get("error") or not expl.get("parts"):
                        raise RuntimeError(expl.get("error") or "no parts")
                except Exception as e:  # noqa: BLE001  the traces may be gone: keep the query without an explanation, say why
                    expl = {"error": f"{type(e).__name__}: {e}"[:400], "for": QS.explanation_tag(rec), "computed_at": round(time.time(), 1)}
                    tally["explain_failed"] += 1
            out = QS.save(c["id"], rec, explanation=expl, user=rec["asked_by"])
            if not out or out.startswith("error"):
                tally["errors"] += 1
                print(f"    {rec['id']}: {out}")
            else:
                tally["kept"] += 1
    tally["permissions"] = QS.fix_permissions()        # every artifact group-shared (setgid folders, g=rw files), so the group can read and ask
    print(json.dumps(tally))


if __name__ == "__main__":
    main()
