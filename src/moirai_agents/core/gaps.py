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


def open_gap(pid: str, answer_id: str, gap_id: str) -> Dict[str, Any]:
    """Keep a gap of an answer on its app (once: the same kind and subject open again returns the open one)."""
    from . import answers as A
    rec = next((a for a in A.listing(pid, limit=10 ** 6) if a.get("id") == answer_id), None)
    if not rec:
        raise P.PipelineError(f"no answer {answer_id} on {pid}")
    listed = rec.get("gaps") or []
    if listed and isinstance(listed[0], str):          # an older, agent-written answer: its gaps are text; "t<i>" names the i-th
        listed = [{"id": f"t{i}", "kind": "reported", "subject": (x.split(";")[0].split(":")[0])[:80], "detail": x} for i, x in enumerate(listed)]
    g = next((x for x in listed if isinstance(x, dict) and x.get("id") == gap_id), None)
    if not g:
        raise P.PipelineError(f"answer {answer_id} has no gap {gap_id}")
    app = _app(pid)
    out: Dict[str, Any] = {}

    def fn(c):
        gs = c.setdefault("gaps", [])
        same = next((x for x in gs if x["kind"] == g["kind"] and x["subject"] == g["subject"] and x["status"] == "open"), None)
        if same:
            same.setdefault("seen_in", []).append({"pipeline": pid, "answer": answer_id, "question": rec.get("question", "")})
            out.update(same)
            return {"kind": "gap_seen", "gap": same["id"]}
        new = {"id": f"G-{gap_id}" if g["kind"] != "reported" else f"G-{answer_id[:6]}{gap_id}", "kind": g["kind"], "subject": g["subject"], "detail": g["detail"], "status": "open", "opened": round(time.time(), 1),
               "from": {"pipeline": pid, "answer": answer_id, "question": rec.get("question", ""), "steps": rec.get("plan", {}).get("steps")}, "seen_in": []}
        gs.append(new)
        out.update(new)
        return {"kind": "gap_open", "gap": new["id"]}
    P.update(app["id"], fn)
    return {**out, "app": app["id"], "chat": app.get("chat_session") or ""}


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
