"""Performance (performance): how to improve efficiency: the bottleneck, time per function and layer, I/O, parallelism, app- vs system-centric. One of the four aspects of provenance (traceability, accountability, decision making, performance),
each its own capability. Its query operations live in moirai_agents.core.aspects.performance and are planned with evidence_answer; every read
goes through prov_read (core.provtrace, TraceViewer)."""
from moirai_agents.core import aspects as AS
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def performance_summary(pipeline: str = "", traces: str = "") -> dict:
        """The performance questions every run should answer, on a pipeline's traces (or a trace dir), through TraceViewer: time per activity, app vs system time, I/O per file, parallelism (processes, hosts, wall time, average concurrency), and the nodes' CPU counters.
        Use it to see what the provenance says about performance; for a user's question plan evidence_answer with the operations in the skill.
        Returns {aspect, label, scope, traces, answers: [{plan, headline, table, gaps}]}."""
        try:
            r = AS.summary("performance", pipeline, traces)
            r.pop("_outs", None)
            return r
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def performance_check(pipeline: str = "", traces: str = "") -> dict:
        """Validator: whether the annotation captured what performance needs (activities timed, I/O traced, system counters, app hot code performance-annotated). Each check names what is missing and the fix (the
        annotation change). Use after the baseline (or any run) and after re-annotating. Returns {aspect, status: pass|warn|fail, failed, advice,
        checks: [{name, ok, required, detail, fix}], capture}."""
        try:
            return AS.check("performance", pipeline, traces)
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def performance_selftest() -> dict:
        """Self-check: the performance summary and check on a synthetic trace that holds every aspect must state the expected facts. Run after any
        change to this capability or to core.aspects.performance. Returns {status, missing, check, failed, line}."""
        return AS.selftest("performance")
