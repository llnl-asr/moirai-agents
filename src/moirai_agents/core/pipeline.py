"""Pipelines: ordered, user-gated stages, one JSON file per pipeline.

    run:       plan -> explore -> smoke -> discover -> annotate -> integrate -> moirai_smoke -> baseline -> graph -> query -> package
    analysis:  plan -> ingest -> dashboard -> query

Each stage is owned by one capability. A stage passes only through :func:`advance`: its validator verdict must
cover the current outputs, the user must have answered its gate, and all their feedback must be resolved. Feedback
Every stage runs predictor -> validator -> corrector. A capability's tool predicts, its validator (a tool, deterministic) rules, and on failure the verdict
sends the agent to the capability's skill, whose Corrector section says how to repair the ORIGINAL tool or its input; then predict and validate again.
Attempts follow progress (:func:`finish_validation`): three to start, more while the failures change, and the same failure twice stops the loop for the user.
Feedback comes in two kinds: ``process`` (how the work is done: becomes a new or updated capability) and ``remember`` (a fact:
becomes a lesson of the owning capability, at once). The plan is kept true here, not by convention: every write syncs each stage's status and result into it,
a failed verdict or process feedback marks that step stale, and a stale step cannot pass until the plan says what changed. State: ``<workspace>/workspaces/pipelines/<id>/pipeline.json`` (beside the
dftracer sessions, under the path dftracer-agents' permission policy allows), written
atomically under a lock, plus an append-only ``events.jsonl``.
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

KINDS: Dict[str, List[str]] = {
    "run": ["plan", "explore", "smoke", "discover", "annotate", "integrate", "moirai_smoke", "baseline", "graph", "query", "package"],
    "analysis": ["plan", "ingest", "dashboard", "query"],
}
OWNERS: Dict[str, str] = {"plan": "run_planner", "package": "repro_package", "explore": "app_explore", "smoke": "app_smoke", "discover": "prov_discover", "annotate": "prov_annotate", "graph": "prov_graph",
                          "integrate": "moirai_integrate", "moirai_smoke": "moirai_run", "baseline": "moirai_run",
                          "ingest": "trace_ingest", "dashboard": "prov_dashboard", "query": "prov_query"}
STATUSES = ("pending", "running", "needs_user", "failed", "passed", "skipped")
FEEDBACK_KINDS = ("process", "remember")
NO_REV = {"progress", "dashboard"}          # presentation only: does not invalidate a stage's verdict


class PipelineError(ValueError):
    pass


def root(workspace: Optional[str] = None) -> Path:
    if os.environ.get("MOIRAI_AGENTS_PIPELINES_DIR"):
        return Path(os.environ["MOIRAI_AGENTS_PIPELINES_DIR"]).expanduser()
    ws = workspace or os.environ.get("MOIRAI_AGENTS_USER_WORKSPACE")
    return (Path(ws) if ws else Path(__file__).resolve().parents[3] / "workspace") / "workspaces" / "pipelines"


def pdir(pid: str, workspace: Optional[str] = None) -> Path:
    if not re.match(r"^[a-z0-9][a-z0-9._-]{2,80}$", pid or ""):
        raise PipelineError(f"bad pipeline id {pid!r}")
    return root(workspace) / pid


@contextlib.contextmanager
def _locked(pid: str):
    d = pdir(pid)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield d
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load(pid: str, workspace: Optional[str] = None) -> Dict[str, Any]:
    p = pdir(pid, workspace) / "pipeline.json"
    if not p.exists():
        raise PipelineError(f"no pipeline {pid!r} under {root(workspace)}")
    return _migrate(json.loads(p.read_text()))


def _migrate(c: Dict[str, Any]) -> Dict[str, Any]:
    """A pipeline saved before a stage existed gets that stage, pending, so old pipelines keep loading."""
    for name in KINDS.get(c.get("kind"), []):
        c["stages"].setdefault(name, _blank(name))
    return c


def _write(d: Path, c: Dict[str, Any]) -> None:
    c["updated"] = round(time.time(), 1)
    _sync_plan(c)
    tmp = d / "pipeline.json.tmp"
    tmp.write_text(json.dumps(c, indent=1, default=str))
    os.replace(tmp, d / "pipeline.json")


def _sync_plan(c: Dict[str, Any]) -> None:
    """The plan shows what is actually happening: each step takes its stage's state, on every write."""
    plan = c.get("plan")
    if not plan:
        return
    for name, step in plan["steps"].items():
        s = c["stages"].get(name) or {}
        v = s.get("validation") or {}
        step.update(status=s.get("status", "pending"), verdict=v.get("status"), result=s.get("outputs", {}).get("summary", ""))
        if v.get("status") == "fail" and v.get("ts", 0) > step.get("revised", 0):
            step["stale"] = f"validator failed: {', '.join(f['name'] for f in v.get('failed', []))[:200]}"
        fb = [x for x in s.get("feedback", []) if x["kind"] == "process" and x["ts"] > step.get("revised", 0)]
        if fb:
            step["stale"] = f"process feedback: {fb[-1]['text'][:200]}"
    plan["updated"] = c["updated"]
    (work(c) / "PLAN.md").write_text(render_plan(c))


def render_plan(c: Dict[str, Any]) -> str:
    plan = c["plan"]
    out = [f"# Plan: {c['name']}", "", f"**Goal:** {plan['goal']}", ""]
    if plan.get("assumptions"):
        out += ["## Assumptions", ""] + [f"- {x}" for x in plan["assumptions"]] + [""]
    if plan.get("questions"):
        out += ["## Open questions", ""] + [f"- {x}" for x in plan["questions"]] + [""]
    out += ["## Steps", ""]
    for name in KINDS[c["kind"]]:
        st = plan["steps"].get(name) or {}
        out += [f"### {name} ({st.get('status', 'pending')})", "", st.get("intent", ""), ""]
        out += [f"- **{k}:** {st[f]}" for k, f in (("How", "approach"), ("Done when", "done_when"), ("Risks", "risks"), ("Result", "result"), ("Needs revising", "stale")) if st.get(f)] + [""]
    if plan.get("revisions"):
        out += ["## Revisions", ""] + [f"- {time.strftime('%F %T', time.localtime(r['ts']))} {r['stage']}: {r['why']}" for r in plan["revisions"]] + [""]
    return "\n".join(out)


def write_plan(pid: str, goal: str, steps: Dict[str, Any], assumptions: List[str], questions: List[str]) -> Dict[str, Any]:
    def fn(c):
        c["plan"] = {"goal": goal, "steps": {k: dict(v) for k, v in steps.items()}, "assumptions": assumptions, "questions": questions, "revisions": []}
        s = c["stages"]["plan"]
        s["status"] = "running"
        s["rev"] += 1
        s["outputs"]["summary"] = f"{len(steps)} steps: {goal[:80]}"
        return {"kind": "plan"}
    return update(pid, fn)


def update_plan(pid: str, name: str, why: str, fields: Dict[str, str]) -> Dict[str, Any]:
    """A revision: what changed and why, kept for good. It clears the stale mark."""
    if not (why or "").strip():
        raise PipelineError("say what changed")
    out = {}

    def fn(c):
        plan = c.get("plan")
        if not plan:
            raise PipelineError("no plan yet: plan_write")
        if name not in plan["steps"]:
            raise PipelineError(f"the plan has no stage {name!r}; stages: {list(plan['steps'])}")
        st, now = plan["steps"][name], round(time.time(), 1)
        st.update(fields)
        st.pop("stale", None)
        st["revised"] = now
        plan["revisions"].append({"ts": now, "stage": name, "why": why.strip()[:500], "changed": sorted(fields)})
        out.update(status="ok", stage=name, revisions=len(plan["revisions"]))
        return {"kind": "plan_update", "stage": name}
    update(pid, fn)
    return out


def _event(d: Path, kind: str, **data) -> None:
    with open(d / "events.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": round(time.time(), 1), "kind": kind, **data}, default=str) + "\n")


def update(pid: str, fn: Callable[[Dict[str, Any]], Optional[dict]]) -> Dict[str, Any]:
    """Read-modify-write under the lock; ``fn(c)`` mutates c (and may return an event dict)."""
    with _locked(pid) as d:
        c = load(pid)
        ev = fn(c)
        _write(d, c)
        if ev:
            _event(d, **ev)
        return c


def _blank(name: str) -> Dict[str, Any]:
    return {"name": name, "status": "pending", "owner": OWNERS[name], "outputs": {}, "validation": None,
            "gate": None, "feedback": [], "history": [], "rev": 0}


def new(kind: str, name: str, source: str = "", command: str = "", traces: str = "", notes: str = "") -> Dict[str, Any]:
    if kind not in KINDS:
        raise PipelineError(f"kind must be one of {sorted(KINDS)}")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$", name or ""):
        raise PipelineError("name must be short: letters, digits, . _ -")
    if kind == "run" and not (source.strip() and command.strip()):
        raise PipelineError("a run pipeline needs the app source directory and a small test command")
    if kind == "analysis" and not traces.strip():
        raise PipelineError("an analysis pipeline needs the trace directory or glob from the user")
    pid = f"{name.lower()}-{time.strftime('%Y%m%d-%H%M%S')}"
    c = {"id": pid, "kind": kind, "name": name, "created": round(time.time(), 1), "source": source.strip(),
         "command": command.strip(), "traces": traces.strip(), "notes": notes, "dashboard": None,
         "chat_session": os.environ.get("MOIRAI_AGENTS_WEBUI_SESSION", ""), "user": os.environ.get("MOIRAI_AGENTS_USER", ""),
         "decisions": [], "stages": {s: _blank(s) for s in KINDS[kind]}}
    with _locked(pid) as d:
        if (d / "pipeline.json").exists():
            raise PipelineError(f"pipeline {pid} already exists")
        _write(d, c)
        _event(d, "created", pipeline_kind=kind, name=name)
    return c


def list_all(chat_session: str = "", workspace: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    r = root(workspace)
    for p in sorted(r.glob("*/pipeline.json")) if r.exists() else []:
        try:
            c = _migrate(json.loads(p.read_text()))
        except ValueError:
            continue
        if not chat_session or c.get("chat_session") == chat_session:
            out.append(c)
    return sorted(out, key=lambda c: c.get("created", 0), reverse=True)


def stage(c: Dict[str, Any], name: str) -> Dict[str, Any]:
    if name not in c["stages"]:
        raise PipelineError(f"pipeline {c['id']} has no stage {name!r}; stages: {KINDS[c['kind']]}")
    return c["stages"][name]


def require_ready(c: Dict[str, Any], name: str) -> None:
    order = KINDS[c["kind"]]
    missing = [f"{p} ({c['stages'][p]['status']})" for p in order[:order.index(name)]
               if c["stages"][p]["status"] not in ("passed", "skipped")]
    if missing:
        raise PipelineError(f"stage {name} cannot start before: {', '.join(missing)}")


def record(pid: str, name: str, status: Optional[str] = None, outputs: Optional[dict] = None,
           validation: Optional[dict] = None, note: str = "") -> Dict[str, Any]:
    """What a predictor or validator produced (outputs merge). A verdict is only valid for the outputs it checked."""
    if status == "passed":
        raise PipelineError("a stage passes only through advance()")
    if status and status not in STATUSES:
        raise PipelineError(f"status must be one of {STATUSES}")

    def fn(c):
        s = stage(c, name)
        if status:
            s["status"] = status
            s.setdefault("started", round(time.time(), 1))
        if outputs:
            s["outputs"].update(outputs)
            if set(outputs) - NO_REV:
                s["rev"] += 1
        if validation is not None:
            s["validation"] = {**validation, "rev": s["rev"], "ts": round(time.time(), 1)}
        if note:
            s["history"].append({"ts": round(time.time(), 1), "note": note[:500]})
        return {"kind": "stage", "stage": name, "status": status, "note": note[:200]}
    return update(pid, fn)


class Verdict:
    """A validator's findings: each check names what is wrong and how to fix it, so the agent need not re-diagnose. Same input, same verdict:
    no model in the loop. ``assume`` is what must hold for the checks to mean anything: when it does not, the result is inconclusive, never a failure."""

    def __init__(self, stage_name: str) -> None:
        self.stage, self.checks = stage_name, []

    def check(self, name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail[:400], "fix": "" if ok else fix[:400]})
        return bool(ok)

    def assume(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": True if ok else None, "detail": detail[:400], "fix": ""})
        return bool(ok)

    def inconclusive(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "ok": None, "detail": detail[:400], "fix": ""})

    def result(self) -> Dict[str, Any]:
        failed = [c for c in self.checks if c["ok"] is False]
        unknown = [c for c in self.checks if c["ok"] is None]
        return {"status": "fail" if failed else "inconclusive" if unknown else "pass", "stage": self.stage,
                "failed": failed, "unknown": unknown, "passed": sum(1 for c in self.checks if c["ok"]), "total": len(self.checks)}


START_ATTEMPTS, MAX_ATTEMPTS = 3, 5


def _attempts(s: Dict[str, Any], r: Dict[str, Any]) -> Dict[str, Any]:
    """The corrector loop's budget, from measured progress: a changed failure earns an attempt, the same failure twice ends it."""
    a = s.setdefault("attempts", {"count": 0, "budget": START_ATTEMPTS, "last": None})
    if r["status"] == "pass":
        a["last"] = None
        return {"state": "done", **a}
    if r["status"] == "inconclusive":
        return {"state": "inconclusive", **a}
    sig = sorted(f["name"] for f in r["failed"])
    a["count"] += 1
    if a["last"] == sig:
        return {"state": "stalled", **a}
    if a["last"] is not None and len(sig) < len(a["last"]):
        a["budget"] = min(MAX_ATTEMPTS, a["budget"] + 2)     # fewer checks fail: closer
    elif a["last"] is not None:
        a["budget"] = min(MAX_ATTEMPTS, a["budget"] + 1)     # a different failure: the idea moved
    a["last"] = sig
    return {"state": "exhausted" if a["count"] >= a["budget"] else "correcting", **a}


def finish_validation(pid: str, v: Verdict) -> Dict[str, Any]:
    """Record the verdict and return it with what comes next: the gate, or the corrector."""
    r = v.result()
    state = {}

    def fn(c):
        s = stage(c, v.stage)
        state.update(_attempts(s, r), owner=s["owner"])
        s["status"] = "needs_user" if r["status"] == "pass" else "running" if r["status"] == "inconclusive" else "failed"
        s["validation"] = {**r, "rev": s["rev"], "ts": round(time.time(), 1)}
        s["history"].append({"ts": round(time.time(), 1), "note": f"validator: {r['status']} ({r['passed']}/{r['total']})"})
        return {"kind": "validation", "stage": v.stage, "status": r["status"]}
    update(pid, fn)
    if r["status"] == "pass":
        r["next"] = "gate: show the user what was produced with ask_user(pipeline, stage)"
    elif state["state"] in ("stalled", "exhausted"):
        r["next"] = f"stop correcting ({state['state']} after {state['count']} attempts): tell the user what both attempts assumed, and ask"
    else:
        r["next"] = (f"corrector: load skill {state['owner']}, section Corrector, apply each failed check's fix to the original tool's input (or capability_update if the tool itself "
                     f"is wrong), then run the predictor and this validator again. Attempt {state['count']} of {state['budget']}.")
    r["corrector"] = {"skill": state["owner"], **{k: state[k] for k in ("state", "count", "budget")}}
    return r


def decide(pid: str, name: str, approved: bool, user_answer: str) -> Dict[str, Any]:
    """The user's answer at a stage's gate, verbatim."""
    if not (user_answer or "").strip():
        raise PipelineError("a gate is answered by the user: pass their answer verbatim")

    def fn(c):
        s = stage(c, name)
        s["gate"] = {"approved": bool(approved), "answer": user_answer.strip()[:2000], "ts": round(time.time(), 1)}
        c["decisions"].append({"stage": name, **s["gate"]})
        return {"kind": "decision", "stage": name, "approved": bool(approved)}
    return update(pid, fn)


def advance(pid: str, name: str) -> Dict[str, Any]:
    """Pass a stage: a passing verdict of the CURRENT outputs, an approving gate, all feedback resolved."""
    def fn(c):
        s = stage(c, name)
        require_ready(c, name)
        v = s.get("validation") or {}
        if v.get("status") != "pass":
            raise PipelineError(f"{name}: validator verdict is {v.get('status') or 'missing'}; run its validator and fix what it lists")
        if v.get("rev") != s["rev"]:
            raise PipelineError(f"{name}: outputs changed after the last validation; validate again")
        open_fb = [x["id"] for x in s["feedback"] if not x.get("resolution")]
        if open_fb:
            raise PipelineError(f"{name}: user feedback {open_fb} not acted on yet")
        if not (s.get("gate") or {}).get("approved"):
            raise PipelineError(f"{name}: waiting for the user at the gate")
        stale = ((c.get("plan") or {}).get("steps", {}).get(name) or {}).get("stale")
        if stale:
            raise PipelineError(f"{name}: the plan is out of date ({stale}): plan_update says what changed")
        s["status"], s["finished"] = "passed", round(time.time(), 1)
        return {"kind": "advance", "stage": name}
    return update(pid, fn)


def feedback(pid: str, name: str, kind: str, text: str) -> Dict[str, Any]:
    """Record feedback at a gate. ``remember`` is resolved at once by the caller writing the lesson."""
    if kind not in FEEDBACK_KINDS:
        raise PipelineError(f"feedback kind must be one of {FEEDBACK_KINDS}")
    if not (text or "").strip():
        raise PipelineError("feedback is the user's words: pass them verbatim")
    rec = {"id": f"fb-{int(time.time() * 1000) % 10**10}", "ts": round(time.time(), 1), "kind": kind, "stage": name,
           "text": text.strip()[:4000], "capability": OWNERS.get(name, ""), "resolution": None}

    def fn(c):
        stage(c, name)["feedback"].append(rec)
        return {"kind": "feedback", "stage": name, "id": rec["id"], "feedback_kind": kind}
    update(pid, fn)
    return rec


def feedback_resolve(pid: str, name: str, fid: str, resolution: str, ref: str) -> Dict[str, Any]:
    """Close feedback: capability (ref = proposal id), lesson (ref = lesson id), applied_here or no_change (ref = why)."""
    if resolution not in ("capability", "lesson", "applied_here", "no_change"):
        raise PipelineError("resolution must be capability, lesson, applied_here or no_change")
    if not (ref or "").strip():
        raise PipelineError("ref: the proposal id, lesson id, or the reason")

    def fn(c):
        it = next((x for x in stage(c, name)["feedback"] if x["id"] == fid), None)
        if not it:
            raise PipelineError(f"no feedback {fid} on stage {name}")
        it["resolution"] = {"kind": resolution, "ref": ref[:500], "ts": round(time.time(), 1)}
        return {"kind": "feedback_resolved", "stage": name, "id": fid}
    update(pid, fn)
    return {"status": "ok", "id": fid, "resolution": resolution}


def set_dashboard(pid: str, traces: str, live: bool) -> Dict[str, Any]:
    """Point the Dashboard tab at a run's raw trace folder below the pipelines root, where dfdashboard is watching."""
    def fn(c):
        c["dashboard"] = {"traces": traces, "live": bool(live), "ts": round(time.time(), 1)}
        return {"kind": "dashboard", "traces": traces, "live": bool(live)}
    return update(pid, fn)


def link_traces(traces: str, raw: Path) -> int:
    """Make traces from anywhere readable where dfdashboard watches: <root>/<run>/raw. A folder of traces is linked whole, so files
    that appear in it later are seen too; a glob or a tree is linked file by file. Nothing is copied. Returns the number of files."""
    t = os.path.abspath(os.path.expanduser(traces))
    raw.parent.mkdir(parents=True, exist_ok=True)
    if os.path.isdir(t) and glob.glob(os.path.join(glob.escape(t), "*.pfw.gz")):
        if raw.is_symlink():
            raw.unlink()
        if not raw.exists():
            raw.symlink_to(t, target_is_directory=True)
        return len(glob.glob(os.path.join(glob.escape(t), "*.pfw.gz")))
    if os.path.isdir(t):
        files = glob.glob(os.path.join(glob.escape(t), "**", "*.pfw.gz"), recursive=True)
    else:
        files = [f for f in glob.glob(t, recursive=True) if f.endswith(".pfw.gz")]
    raw.mkdir(exist_ok=True)
    base = os.path.commonpath(files) if len(files) > 1 else os.path.dirname(t)
    for f in files:
        link = raw / re.sub(r"[^\w.-]", "_", os.path.relpath(f, os.path.dirname(base)))
        if not link.exists():
            link.symlink_to(f)
    return len(files)


def open_traces(traces: str, workspace: Optional[str] = None) -> str:
    """Open traces with no pipeline: returns the dfdashboard run they appear as, the same run for the same path."""
    t = os.path.abspath(os.path.expanduser(traces))
    name = re.sub(r"[^\w.-]+", "_", os.path.basename(t.rstrip("/*")) or "traces")[:40]
    run = f"opened/{name}-{hashlib.sha1(t.encode()).hexdigest()[:8]}"
    if not link_traces(traces, dash_root(workspace) / run / "raw"):
        raise PipelineError(f"no .pfw.gz trace under {traces}")
    return run


def dash_root(workspace: Optional[str] = None) -> Path:
    """What dfdashboard watches: every session and pipeline of this workspace."""
    return root(workspace).parent


def dash_run(c: Dict[str, Any], workspace: Optional[str] = None) -> str:
    """The run dfdashboard knows this pipeline's traces by: the path below its watch root, without the raw folder."""
    return traces_run((c.get("dashboard") or {}).get("traces"), workspace)


def traces_run(traces: Optional[str], workspace: Optional[str] = None) -> str:
    """The dfdashboard run of a raw trace folder."""
    return os.path.relpath(os.path.dirname(traces.rstrip("/")), dash_root(workspace)) if traces else ""


def set_session(pid: str, session: Dict[str, Any]) -> Dict[str, Any]:
    """Bind the dftracer session this pipeline runs in."""
    def fn(c):
        c["session"] = {"run_id": session["run_id"], "workspace": session["workspace"]}
        return {"kind": "session", "run_id": session["run_id"]}
    return update(pid, fn)


def work(c: Dict[str, Any]) -> Path:
    """Where this pipeline writes: <session>/provenance beside the session's own folders, or the pipeline folder when there is no session."""
    s = c.get("session")
    d = Path(s["workspace"]) / "provenance" if s else pdir(c["id"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def source_of(c: Dict[str, Any]) -> str:
    """The application tree to read: the session's copy, or the user's."""
    s = c.get("session")
    return str(Path(s["workspace"]) / "source") if s else c["source"]


def stage_state(c: Dict[str, Any], name: str) -> Dict[str, Any]:
    """What happened in one stage, for its state page."""
    s = stage(c, name)
    v = s.get("validation") or {}
    p = (c.get("plan") or {}).get("steps", {}).get(name) or {}
    o = s["outputs"]
    return {"pipeline": c["id"], "name": name, "status": s["status"], "owner": s["owner"], "started": s.get("started"), "summary": o.get("summary", ""),
            "command": o.get("command", ""), "plan": p.get("intent", ""), "plan_stale": bool(p.get("stale")), "gate": s.get("gate"), "history": s["history"],
            "feedback": s["feedback"], "checks": {k: v.get(k) for k in ("status", "passed", "total", "failed", "unknown")}}


def summary(c: Dict[str, Any]) -> Dict[str, Any]:
    """The compact view the Steps panel and the agents read."""
    rows = []
    for name in KINDS[c["kind"]]:
        s = c["stages"][name]
        v = s.get("validation") or {}
        rows.append({"name": name, "status": s["status"], "owner": s["owner"], "verdict": v.get("status"),
                     "failed": [f["name"] + ": " + f["detail"] for f in v.get("failed", [])][:6], "plan": ((c.get("plan") or {}).get("steps", {}).get(name) or {}).get("intent", ""),
                     "plan_stale": bool(((c.get("plan") or {}).get("steps", {}).get(name) or {}).get("stale")),
                     "summary": s["outputs"].get("summary", ""), "gate": s.get("gate"),
                     "feedback_open": len([x for x in s["feedback"] if not x.get("resolution")]),
                     "progress": s["outputs"].get("progress")})
    current = next((r["name"] for r in rows if r["status"] not in ("passed", "skipped")), None)
    return {"id": c["id"], "kind": c["kind"], "name": c["name"], "created": c["created"], "updated": c.get("updated"),
            "chat_session": c.get("chat_session"), "current": current, "stages": rows, "dashboard": c.get("dashboard"), "session": c.get("session"), "goal": (c.get("plan") or {}).get("goal", "")}
