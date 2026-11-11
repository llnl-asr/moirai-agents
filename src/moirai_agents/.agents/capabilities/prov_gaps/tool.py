"""Provenance gaps (core.gaps): what a question showed the provenance cannot explain, explored in the app's code during the annotation
phase, fixed in the annotation, and learned twice - generically in the provenance capability, and for the workload."""
from moirai_agents.core import gaps as G
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def gap_list(pipeline: str) -> dict:
        """The gaps opened on an app (a run pipeline, or a live run: its app): each with its kind, subject, what is missing, the question that
        showed it, and its status (open, fixed_pending_run, closed). Use at the start of a gap's work. Returns {app, gaps}."""
        try:
            return {"app": P.origin(P.load(pipeline))["id"], "gaps": G.listing(pipeline)}
        except P.PipelineError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def gap_collect(pipeline: str) -> dict:
        """Collect the gaps of EVERY query asked about the app (its own answers, its live runs', its published datasets') onto the app, so all
        of them are worked on together: one gap per kind and subject, a gap already fixed after the answer that showed it stays fixed.
        Returns {app, chat, added, seen, answers, open}."""
        try:
            return G.collect(pipeline)
        except P.PipelineError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def gap_open(pipeline: str, answer_id: str, gap_id: str) -> dict:
        """Open a gap of a computed answer (evidence_answer's gaps) on the app it came from, to be fixed in the annotation phase. The Query view
        does this from its 'find and fix' button; call it when the user asks you to fix a gap from the chat. Returns the gap, with app and chat."""
        try:
            return G.open_gap(pipeline, answer_id, gap_id)
        except P.PipelineError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def gap_close(pipeline: str, gap_id: str, cause: str, generic: dict, workload: dict, how: str, recheck_answer: str = "", recheck_pipeline: str = "") -> dict:
        """Close a gap with what was learned, after fixing it. cause: missed_inputs | missed_outputs | missed_variables | missed_attributes |
        missed_function | missed_entity | special_structure | other. generic: {capability, version} of the update to the provenance capability
        (prov_annotate, usually) that handles this structure in ANY app. workload: {capability, version} of the update to workload_<app> that
        names it for this app. how: what the annotation missed, why (the code structure), and what now captures it. recheck_answer: an
        evidence_answer run after re-annotating and re-running (on recheck_pipeline, default the app) that no longer shows the gap; without it
        the gap is fixed_pending_run. Returns {status, state} or {status: error, problems}."""
        try:
            return G.close(pipeline, gap_id, cause, generic, workload, how, recheck_answer, recheck_pipeline)
        except P.PipelineError as e:
            return {"status": "error", "problems": [str(e)]}

    @mcp.tool()
    def gap_validate(pipeline: str) -> dict:
        """Validator of an app's gaps: every gap that is not open names its cause, a generic and a workload capability update that are installed,
        and how it was fixed; a closed one names the answer that no longer shows it. Open gaps are listed, not failed. Returns the verdict."""
        gs = G.listing(pipeline)
        v = P.Verdict("gaps")
        done = [g for g in gs if g["status"] != "open"]
        for g in done:
            ok = all((G._cap_version((g.get(k) or {}).get("capability", "")) or -1) >= int((g.get(k) or {}).get("version") or 0) for k in ("generic", "workload"))
            v.check(f"{g['id']}_learned", ok, f"{g['id']}: the capability updates it names are not installed", "update them, then gap_close again")
            if g["status"] == "closed":
                v.check(f"{g['id']}_rechecked", bool(g.get("recheck")), f"{g['id']}: no re-check answer", "run the question again after re-annotating")
        v.check("open", True, f"{len([g for g in gs if g['status'] == 'open'])} open, {len(done)} fixed" if gs else "no gap")
        return v.result()
