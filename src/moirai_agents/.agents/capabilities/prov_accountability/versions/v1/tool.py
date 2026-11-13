"""Accountability (accountability): who did what, when, and with what: the person and their group, job, hosts, processes, commands, tools and versions. One of the four aspects of provenance (traceability, accountability, decision making, performance),
each its own capability. Its query operations live in moirai_agents.core.aspects.accountability and are planned with evidence_answer; every read
goes through prov_read (core.provtrace, TraceViewer)."""
from moirai_agents.core import aspects as AS
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def accountability_summary(pipeline: str = "", traces: str = "") -> dict:
        """The accountability questions every run should answer, on a pipeline's traces (or a trace dir), through TraceViewer: who ran it (resolved from hashes), group, job, hosts, every process (parent, start, executable, command, working dir, dftracer version), and when each activity ran.
        Use it to see what the provenance says about accountability; for a user's question plan evidence_answer with the operations in the skill.
        Returns {aspect, label, scope, traces, answers: [{plan, headline, table, gaps}]}."""
        try:
            r = AS.summary("accountability", pipeline, traces)
            r.pop("_outs", None)
            return r
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def accountability_check(pipeline: str = "", traces: str = "") -> dict:
        """Validator: whether the annotation captured what accountability needs (who ran it, processes and hosts recorded, activities timed). Each check names what is missing and the fix (the
        annotation change). Use after the baseline (or any run) and after re-annotating. Returns {aspect, status: pass|warn|fail, failed, advice,
        checks: [{name, ok, required, detail, fix}], capture}."""
        try:
            return AS.check("accountability", pipeline, traces)
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def accountability_selftest() -> dict:
        """Self-check: the accountability summary and check on a synthetic trace that holds every aspect must state the expected facts. Run after any
        change to this capability or to core.aspects.accountability. Returns {status, missing, check, failed, line}."""
        return AS.selftest("accountability")
