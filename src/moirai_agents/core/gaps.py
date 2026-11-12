"""Provenance gaps: what a question showed the provenance cannot explain, fixed where it came from - the annotation - and learned.

A computed answer (core.evidence) lists its gaps: an entity referenced but never declared, an entity nothing recorded producing (a missed
function: a subprocess, a callback, generated code), an activity without its inputs or outputs, something the question names that is not
recorded. From the Query view the user opens one: it is kept on the app (the run pipeline that annotated it; a live run's parent), and the
app's Annotate chat explores the code to find why. Closing it needs the cause, the GENERIC fix (the provenance capability, so any app with
the same structure is handled), the WORKLOAD fix (the app's workload capability, for recall), and, when the traces were re-made, the
answer that shows the gap gone.

State: the app pipeline's ``gaps`` list.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import pipeline as P

CAUSES = ("missed_inputs", "missed_outputs", "missed_variables", "missed_attributes", "missed_function", "missed_entity", "special_structure", "other")
STATUSES = ("open", "closed", "fixed_pending_run")


def _app(pid: str) -> Dict[str, Any]:
    c = P.load(pid)
    if c["kind"] == "analysis" and c.get("parent"):     # a published run opened as a dataset: its gaps are fixed on the app that published it
        c = P.load(c["parent"])
    if c["kind"] not in ("run", "live"):
        raise P.PipelineError("a dataset has no annotation here to fix: open the gap on the app that produced it")
    return P.origin(c)


def listing(pid: str) -> List[Dict[str, Any]]:
    return list(_app(pid).get("gaps") or [])


def _answer_gaps(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An answer's gaps as dicts; an older, agent-written answer's gaps are text: "t<i>" names the i-th."""
    listed = rec.get("gaps") or []
    if listed and isinstance(listed[0], str):
        listed = [{"id": f"t{i}", "kind": "reported", "subject": (x.split(";")[0].split(":")[0])[:80], "detail": x} for i, x in enumerate(listed)]
    return [x for x in listed if isinstance(x, dict) and x.get("kind")]


def _keep(gs: List[Dict[str, Any]], g: Dict[str, Any], pid: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Keep one answer gap in an app's gap list: the open gap of the same kind and subject (it is seen again), or a new one. None when the
    same gap was already fixed after this answer was asked (an older answer: the fix is newer than what it saw)."""
    answer_id = rec.get("id", "")
    same = next((x for x in gs if x["kind"] == g["kind"] and x["subject"] == g["subject"] and x["status"] == "open"), None)
    if same:
        seen = same.setdefault("seen_in", [])
        frm = same.get("from") or {}
        if (frm.get("pipeline"), frm.get("answer")) != (pid, answer_id) and not any((s.get("pipeline"), s.get("answer")) == (pid, answer_id) for s in seen):
            seen.append({"pipeline": pid, "answer": answer_id, "question": rec.get("question", "")})
        return same
    if any(x["kind"] == g["kind"] and x["subject"] == g["subject"] and x["status"] != "open" and float(x.get("closed") or 0) >= float(rec.get("ts") or 0)
           for x in gs):
        return None
    gid = f"G-{g['id']}" if g["kind"] != "reported" else f"G-{answer_id[:6]}{g['id']}"
    if any(x["id"] == gid for x in gs):                   # fixed before, shown again by a newer answer: open it again under a new id
        gid = f"{gid}-{sum(1 for x in gs if x['id'].startswith(gid))}"
    new = {"id": gid, "kind": g["kind"], "subject": g["subject"], "detail": g.get("detail", ""), "status": "open", "opened": round(time.time(), 1),
           "from": {"pipeline": pid, "answer": answer_id, "question": rec.get("question", ""), "steps": (rec.get("plan") or {}).get("steps")}, "seen_in": []}
    gs.append(new)
    return new


def open_gap(pid: str, answer_id: str, gap_id: str) -> Dict[str, Any]:
    """Keep a gap of an answer on its app (once: the same kind and subject open again returns the open one)."""
    from . import answers as A
    rec = next((a for a in A.listing(pid, limit=10 ** 6) if a.get("id") == answer_id), None)
    if not rec:
        raise P.PipelineError(f"no answer {answer_id} on {pid}")
    g = next((x for x in _answer_gaps(rec) if x.get("id") == gap_id), None)
    if not g:
        raise P.PipelineError(f"answer {answer_id} has no gap {gap_id}")
    app = _app(pid)
    out: Dict[str, Any] = {}

    def fn(c):
        gs = c.setdefault("gaps", [])
        n = len(gs)
        kept = _keep(gs, g, pid, {**rec, "ts": time.time()})     # opened by hand: the user wants it worked on, even if it was fixed before
        out.update(kept or {})
        return {"kind": "gap_open" if len(gs) > n else "gap_seen", "gap": (kept or {}).get("id")}
    P.update(app["id"], fn)
    return {**out, "app": app["id"], "chat": app.get("chat_session") or ""}


def related(app_id: str) -> List[str]:
    """Every pipeline whose questions are about this app: the app, its live runs, and the datasets published from either."""
    allp = {c["id"]: c for c in P.list_all("")}

    def app_of(c: Optional[Dict[str, Any]]) -> Optional[str]:
        if c and c.get("kind") == "analysis" and c.get("parent"):
            c = allp.get(c["parent"])
        if not c or c.get("kind") not in ("run", "live"):
            return None
        return c["parent"] if c.get("kind") == "live" and c.get("parent") else c["id"]
    return [i for i, c in allp.items() if app_of(c) == app_id]


def collect(pid: str) -> Dict[str, Any]:
    """Collect the gaps of every query asked about the app (its own, its live runs', its published datasets') onto the app, so they can be
    worked on together: the same kind and subject is one gap (seen again), and a gap fixed after the answer that showed it is not reopened.
    Returns {app, chat, added, seen, answers, open: [gap]}."""
    from . import answers as A
    app = _app(pid)
    found, once = [], set()
    for p in related(app["id"]):
        for rec in A.listing(p, limit=10 ** 6):      # pipelines on the same traces list each other's queries: each answer once, on its own pipeline
            if rec.get("id") in once:
                continue
            once.add(rec.get("id"))
            found += [(rec.get("pipeline") or p, rec, g) for g in _answer_gaps(rec)]
    out: Dict[str, Any] = {"added": 0, "seen": 0}

    def fn(c):
        gs = c.setdefault("gaps", [])
        before = {x["id"] for x in gs}
        for p, rec, g in found:
            k = _keep(gs, g, p, rec)
            if k is not None and k["id"] in before:
                out["seen"] += 1
        out["added"] = len({x["id"] for x in gs} - before)
        out["open"] = [x for x in gs if x["status"] == "open"]
        return {"kind": "gap_collect", "added": out["added"]}
    P.update(app["id"], fn)
    return {**out, "app": app["id"], "chat": app.get("chat_session") or "", "answers": len({(p, r.get("id")) for p, r, _ in found})}


def _cap_version(name: str) -> Optional[int]:
    """The installed version of a capability (any layer), or None when there is none. A change still waiting for the user's approval
    does not count: the fix is the installed one."""
    from . import capstore as S
    hit = S.all_caps().get(name)
    return int(hit[2].get("version", 0)) if hit else None


def close(app_pid: str, gap_id: str, cause: str, generic: Dict[str, Any], workload: Dict[str, Any], how: str,
          recheck_answer: str = "", recheck_pipeline: str = "") -> Dict[str, Any]:
    """Close a gap with what was learned. generic / workload: {capability, version} of the updates made (the versions must exist).
    recheck_answer: an answer computed after the fix (on recheck_pipeline, default the app) that no longer shows this gap; without it the
    gap is fixed_pending_run (fixed in the capabilities and the annotation, to be confirmed by the next run)."""
    from . import answers as A
    bad = [] if cause in CAUSES else [f"cause must be one of {CAUSES}"]
    for label, u in (("generic", generic), ("workload", workload)):
        if not isinstance(u, dict) or not u.get("capability"):
            bad.append(f"{label}: give {{capability, version}} of the update you made")
            continue
        have = _cap_version(u["capability"])
        if have is None:
            bad.append(f"{label}: no capability {u['capability']!r}")
        elif int(u.get("version") or 0) > have:
            bad.append(f"{label}: {u['capability']} is at v{have}, not v{u.get('version')}: update it first (capability_update / capability_new)")
    if isinstance(workload, dict) and workload.get("capability") and not str(workload["capability"]).startswith("workload_"):
        bad.append("workload: the app's workload capability is named workload_<app>")
    if len((how or "").strip()) < 30:
        bad.append("how: say what the annotation missed, why (the code structure), and what now captures it")
    app = _app(app_pid)
    g = next((x for x in app.get("gaps") or [] if x["id"] == gap_id), None)
    if not g:
        bad.append(f"no gap {gap_id} on {app['id']}")
    status = "fixed_pending_run"
    if recheck_answer and g:
        rp = recheck_pipeline or app["id"]
        rec = next((a for a in A.listing(rp, limit=10 ** 6) if a.get("id") == recheck_answer), None)
        if not rec:
            bad.append(f"no answer {recheck_answer} on {rp}")
        elif any(x["kind"] == g["kind"] and x["subject"] == g["subject"] for x in rec.get("gaps") or []):
            bad.append(f"answer {recheck_answer} still shows this gap: the fix did not reach the traces yet")
        else:
            status = "closed"
    if bad:
        return {"status": "error", "problems": bad}

    def fn(c):
        x = next(y for y in c["gaps"] if y["id"] == gap_id)
        x.update(status=status, cause=cause, generic=generic, workload=workload, how=how.strip(), closed=round(time.time(), 1),
                 **({"recheck": {"pipeline": recheck_pipeline or app["id"], "answer": recheck_answer}} if status == "closed" else {}))
        return {"kind": "gap_close", "gap": gap_id, "status": status}
    P.update(app["id"], fn)
    return {"status": "ok", "gap": gap_id, "state": status}
