"""Decision making (decision): why trust the result: quality, quantity, metrics, what failed or passed. One of the four aspects of provenance (traceability, accountability, decision making, performance),
each its own capability. Its query operations live in moirai_agents.core.aspects.decision and are planned with evidence_answer; every read
goes through prov_read (core.provtrace, TraceViewer)."""
from moirai_agents.core import aspects as AS
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def decision_summary(pipeline: str = "", traces: str = "") -> dict:
        """The decision questions every run should answer, on a pipeline's traces (or a trace dir), through TraceViewer: every recorded outcome (return codes, status flags, errors) by value with the failures first, and the entity types (including the quality attributes) recorded.
        Use it to see what the provenance says about decision; for a user's question plan evidence_answer with the operations in the skill.
        Returns {aspect, label, scope, traces, answers: [{plan, headline, table, gaps}]}."""
        try:
            r = AS.summary("decision", pipeline, traces)
            r.pop("_outs", None)
            return r
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def decision_check(pipeline: str = "", traces: str = "") -> dict:
        """Validator: whether the annotation captured what decision needs (outcomes and quality metrics recorded). Each check names what is missing and the fix (the
        annotation change). Use after the baseline (or any run) and after re-annotating. Returns {aspect, status: pass|warn|fail, failed, advice,
        checks: [{name, ok, required, detail, fix}], capture}."""
        try:
            return AS.check("decision", pipeline, traces)
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def decision_selftest() -> dict:
        """Self-check: the decision summary and check on a synthetic trace that holds every aspect must state the expected facts. Run after any
        change to this capability or to core.aspects.decision. Returns {status, missing, check, failed, line}."""
        return AS.selftest("decision")
