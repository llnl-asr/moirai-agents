"""Run the two provenance pipelines: create them, read their state, and hold every stage at the user's gate."""
from moirai_agents.core import capstore as S
from moirai_agents.core import dfbridge as B
from moirai_agents.core import jobs as J
from moirai_agents.core import learning as L
from moirai_agents.core import pipeline as P


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
        """Record the user's answer at a gate, verbatim. Use only with what the user said; never approve for them.
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
    def ask_user(question: str, kind: str = "choose", options: list = None, context: str = "", pipeline: str = "", stage: str = "") -> dict:
        """Ask the user, and hold a gate. The web UI shows the question, option buttons and a comment box, and at a gate (pipeline and stage given)
        Approve, Change the process and Remember this. After calling it END YOUR TURN: the answer arrives as the next message.
        kind: confirm|choose|multi|freeform. options: [{label, description, recommended}]. Returns {status, question, options}."""
        opts = options or ([{"label": "Yes, go ahead", "recommended": True}, {"label": "No"}] if kind == "confirm" else [])
        if pipeline and stage:
            _safe(P.record, pipeline, stage, status="needs_user", note="waiting at the gate")
        return {"status": "question shown to the user", "question": question, "kind": kind, "options": opts, "context": context,
                "pipeline": pipeline, "stage": stage, "instruction": "End your turn now and wait for the answer in the next message. "
                "In a terminal session (no web UI), ask the same question in your reply."}
