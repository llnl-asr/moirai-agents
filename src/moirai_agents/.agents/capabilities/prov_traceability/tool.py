"""Traceability (traceability): where did this come from: workflow and app instance, process, function, code region, parameters, attributes. One of the four aspects of provenance (traceability, accountability, decision making, performance),
each its own capability. Its query operations live in moirai_agents.core.aspects.traceability and are planned with evidence_answer; every read
goes through prov_read (core.provtrace, TraceViewer)."""
from moirai_agents.core import aspects as AS
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def traceability_summary(pipeline: str = "", traces: str = "") -> dict:
        """The traceability questions every run should answer, on a pipeline's traces (or a trace dir), through TraceViewer: every declared entity by type, and every activity by name.
        Use it to see what the provenance says about traceability; for a user's question plan evidence_answer with the operations in the skill.
        Returns {aspect, label, scope, traces, answers: [{plan, headline, table, gaps}]}."""
        try:
            r = AS.summary("traceability", pipeline, traces)
            r.pop("_outs", None)
            return r
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def traceability_check(pipeline: str = "", traces: str = "") -> dict:
        """Validator: whether the annotation captured what traceability needs (entities declared, activities recorded, parameters and attributes captured). Each check names what is missing and the fix (the
        annotation change). Use after the baseline (or any run) and after re-annotating. Returns {aspect, status: pass|warn|fail, failed, advice,
        checks: [{name, ok, required, detail, fix}], capture}."""
        try:
            return AS.check("traceability", pipeline, traces)
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def traceability_selftest() -> dict:
        """Self-check: the traceability summary and check on a synthetic trace that holds every aspect must state the expected facts. Run after any
        change to this capability or to core.aspects.traceability. Returns {status, missing, check, failed, line}."""
        return AS.selftest("traceability")
