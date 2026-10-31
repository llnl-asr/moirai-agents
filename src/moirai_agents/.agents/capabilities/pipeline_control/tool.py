"""Run the two provenance pipelines: create them, read their state, and hold every stage at the user's gate."""
import os
import re

from moirai_agents.core import capstore as S
from moirai_agents.core import dfbridge as B
from moirai_agents.core import jobs as J
from moirai_agents.core import learning as L
from moirai_agents.core import pipeline as P


AUTO_STOP = re.compile(r"\s*(no\b|reject|stop|cancel|abort|do not|don't)", re.I)
QUESTION_CHARS, CONTEXT_CHARS, CONTEXT_LINE_CHARS = 200, 900, 220


def _ask_problem(question: str, context: str) -> str:
    """Why this is too much to read at a glance, or "" when it is fine."""
    if len(question) > QUESTION_CHARS:
        return f"question is {len(question)} characters; keep it to one sentence of at most {QUESTION_CHARS}: the decision you need"
    if len(context) > CONTEXT_CHARS:
        return f"context is {len(context)} characters; keep at most {CONTEXT_CHARS} and move the rest to details"
    long = max(context.splitlines() or [""], key=len)
    if len(long) > CONTEXT_LINE_CHARS:
        return f"context has a line of {len(long)} characters; use short Markdown bullets of at most {CONTEXT_LINE_CHARS}, and move the rest to details"
    return ""


def _auto_answer(kind: str, opts: list, pipeline: str, stage: str) -> dict:
    """The answer in auto mode, or {} when this question must wait for the person: nothing is recommended, the stage's checks did not pass, or the recommendation is to stop."""
    if not P.auto_mode(os.environ.get("MOIRAI_AGENTS_WEBUI_SESSION", "")):
        return {}
    picks = [str(o.get("label", "")) for o in opts if isinstance(o, dict) and o.get("recommended")]
    gate = bool(pipeline and stage)
    if gate and not opts:
        picks = [f"Approve {stage}"]
    if not picks or kind == "freeform" or (kind != "multi" and len(picks) > 1):
        return {}
    answer = "; ".join(picks)
    if gate:
        if (P.stage(P.load(pipeline), stage).get("validation") or {}).get("status") != "pass" or AUTO_STOP.match(answer):
            return {}
        P.decide(pipeline, stage, True, f"Auto mode took the recommended option: {answer}")
    return {"status": "answered by auto mode", "answer": answer, "auto": True, "instruction": "The user turned on auto mode: this is their answer. Do not end your turn: "
            + ("pipeline_advance, then go on with the next stage." if gate else "go on with this answer.")}


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except P.PipelineError as e:
        return {"status": "error", "error": str(e)}


def register(mcp):
    @mcp.tool()
    def pipeline_new(kind: str, name: str, source: str = "", command: str = "", traces: str = "", notes: str = "", ref: str = "") -> dict:
        """Create a pipeline. Use once, at the start. kind: run (needs source: app directory or git URL, command: a small test run) or analysis (needs traces: the directory or
        glob the user gave). A run pipeline gets a dftracer session, which holds all its work; ref: git ref of a URL. Returns {id, kind, stages, current, session}, with session_error if the session failed."""
        def go():
            c = P.new(kind, name, source, command, traces, notes)
            if kind != "run" or not B.available():
                return P.summary(c)
            s = B.start_session(name, source, ref, f"moirai pipeline {c['id']}")
            if s.get("status") != "ok":
                return {**P.summary(c), "session_error": s.get("error")}
            return P.summary(P.set_session(c["id"], s))
        return _safe(go)

    @mcp.tool()
    def pipeline_status(pipeline: str = "") -> dict:
        """State of one pipeline, or of every pipeline in this chat. Use at the start of each turn to see which stage is current,
        what its validator said and what the gate waits for. Returns {id, current, stages: [{name, status, owner, verdict, failed, gate}]}."""
        def go():
            if pipeline:
                return {**P.summary(P.load(pipeline)), "jobs": J.list_jobs(pipeline)}
            return {"pipelines": [P.summary(c) for c in P.list_all()]}
        return _safe(go)

    @mcp.tool()
    def pipeline_decide(pipeline: str, stage: str, approved: bool, user_answer: str) -> dict:
        """Record the user's answer at a gate, verbatim. Use only with what the user said; never approve for them. Auto mode records its own answer: do not call this after it.
        Returns {status, stage, approved}."""
        return _safe(lambda: (P.decide(pipeline, stage, approved, user_answer), {"status": "ok", "stage": stage, "approved": approved})[1])

    @mcp.tool()
    def pipeline_advance(pipeline: str, stage: str) -> dict:
        """Pass a stage. Use after its validator passed and the user approved. Refused with the reason otherwise.
        Returns {status, stage, next, standing}: standing is the rule to act on before the next stage."""
        def go():
            c = P.advance(pipeline, stage)
            return {"status": "ok", "stage": stage, "next": P.summary(c)["current"], "standing": S.STANDING}
        return _safe(go)

    @mcp.tool()
    def pipeline_feedback(pipeline: str, stage: str, kind: str, text: str) -> dict:
        """Record what the user said at a gate, verbatim. kind=remember: a fact, written as a lesson of the stage's capability now.
        kind=process: how the work should be done differently: then update or create a capability and call pipeline_feedback_resolve.
        Returns {id, capability, resolution, next}."""
        def go():
            fb = P.feedback(pipeline, stage, kind, text)
            if kind == "remember":
                les = L.add(fb["capability"], text, "user", stage=stage, pipeline=pipeline)
                P.feedback_resolve(pipeline, stage, fb["id"], "lesson", les["id"])
                return {**fb, "resolution": "lesson", "lesson": les["id"], "next": "applied; continue the stage"}
            return {**fb, "next": f"capability_get('{fb['capability']}'), add a check that reproduces it, capability_update (or capability_new if it is a "
                                  "new process), let the user approve, re-run the stage, then pipeline_feedback_resolve"}
        return _safe(go)

    @mcp.tool()
    def pipeline_feedback_resolve(pipeline: str, stage: str, id: str, resolution: str, ref: str) -> dict:
        """Close process feedback. Use once it was acted on. resolution: capability (ref: proposal id), lesson (ref: lesson id), applied_here or
        no_change (ref: why, agreed with the user). Returns {status, id, resolution}."""
        return _safe(P.feedback_resolve, pipeline, stage, id, resolution, ref)

    @mcp.tool()
    def pipeline_validate(pipeline: str) -> dict:
        """Rule on a pipeline's state: every passed stage has a passing verdict of its current outputs and the user's approval, no passed stage has open feedback, stages passed in order.
        Use when something looks off, and at the end of a pipeline. Returns {status, failed, passed, total}."""
        def go():
            c = P.load(pipeline)
            v = P.Verdict("pipeline")
            seen_open = False
            for name in P.KINDS[c["kind"]]:
                s = c["stages"][name]
                if s["status"] != "passed":
                    seen_open = True
                    continue
                vd = s.get("validation") or {}
                v.check(f"{name}:order", not seen_open, "passed after an earlier stage that did not", "re-run the earlier stage")
                v.check(f"{name}:verdict", vd.get("status") == "pass" and vd.get("rev") == s["rev"], str(vd.get("status")), f"run the validator of {name} again")
                v.check(f"{name}:gate", bool((s.get("gate") or {}).get("approved")), "no approval on record", "ask the user")
                v.check(f"{name}:feedback", not [x for x in s["feedback"] if not x.get("resolution")], "open feedback", "act on it, then pipeline_feedback_resolve")
            return v.result()
        return _safe(go)

    @mcp.tool()
    def job_status(pipeline: str, job: str = "") -> dict:
        """State of a pipeline's background jobs, with the log tail for one job. Use to follow long builds and runs.
        Returns {jobs: [...]} or one job {id, status, rc, elapsed, log_tail}."""
        return _safe(lambda: J.get(pipeline, job) if job else {"jobs": J.list_jobs(pipeline)})

    @mcp.tool()
    def ask_user(question: str, kind: str = "choose", options: list = None, context: str = "", pipeline: str = "", stage: str = "", details: str = "") -> dict:
        """Ask the user, and hold a gate. The web UI shows the question, option buttons and a comment box, and at a gate (pipeline and stage given)
        Approve, Change the process and Remember this. After calling it END YOUR TURN: the answer arrives as the next message.
        kind: confirm|choose|multi|freeform. options: [{label, description, recommended}].
        Written for a person deciding at a glance. question: one sentence, the decision. context: short Markdown, a line of the result and bullets of what they must
        know to decide. details: Markdown, shown collapsed: everything else, as headed sections, bullets and tables. Text that is too long is refused with what to change.
        Mark the option you advise with recommended: in auto mode it is taken at once, and the result says status "answered by auto mode": then do not end your turn, go on. A question with
        nothing recommended waits for the person, as does a gate whose checks did not pass.
        Returns {status, question, options}, or {status: error, error}: fix it and call again."""
        problem = _ask_problem(question, context)
        if problem:
            return {"status": "error", "error": f"not shown to the user: {problem}"}
        opts = options or ([{"label": "Yes, go ahead", "recommended": True}, {"label": "No"}] if kind == "confirm" else [])
        auto = _auto_answer(kind, opts, pipeline, stage)
        if auto:
            return auto
        if pipeline and stage:
            _safe(P.record, pipeline, stage, status="needs_user", note="waiting at the gate")
        return {"status": "question shown to the user", "question": question, "kind": kind, "options": opts, "context": context, "details": details,
                "pipeline": pipeline, "stage": stage, "instruction": "End your turn now and wait for the answer in the next message. "
                "In a terminal session (no web UI), ask the same question in your reply."}
