"""tools_nextflow_annotate: annotate a copy of a Nextflow pipeline so each process task reports to moirai (stage 1 stub; the whole tool follows)."""
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def nextflow_annotate_spec(pipeline: str, exclude: list = None, overrides: dict = None) -> dict:
        """Per-process annotation spec of a Nextflow pipeline (stage 1 stub). pipeline: pipeline id. exclude: process names to skip. overrides: per-process patches. Returns {status}."""
        P.load(pipeline)
        return {"status": "stub"}

    @mcp.tool()
    def nextflow_annotate_diagram(pipeline: str) -> dict:
        """Nodes and edges of the annotation for diagram_publish_graph (stage 1 stub). pipeline: pipeline id. Returns {status}."""
        P.load(pipeline)
        return {"status": "stub"}

    @mcp.tool()
    def nextflow_annotate_apply(pipeline: str, spec: dict = None) -> dict:
        """Write marked hook blocks into a copy of the pipeline (stage 1 stub). pipeline: pipeline id. spec: the spec. Returns {status}."""
        P.load(pipeline)
        return {"status": "stub"}

    @mcp.tool()
    def nextflow_annotate_validate(pipeline: str) -> dict:
        """Validate the annotated copy (stage 1 stub). pipeline: pipeline id. Returns {status}."""
        P.load(pipeline)
        return {"status": "stub"}

    @mcp.tool()
    def nextflow_annotate_preview(files: dict, process: str, task: dict = None, spec: dict = None) -> dict:
        """Run one annotated process hook against a fake moirai (stage 1 stub). files: {relative path: module text}. process: name. task: fixture. spec: per-process spec. Returns {status}."""
        return {"status": "stub"}

    @mcp.tool()
    def nextflow_annotate_selftest() -> dict:
        """Self test of the capability (stage 1 stub). Returns {status}."""
        return {"status": "ok"}
