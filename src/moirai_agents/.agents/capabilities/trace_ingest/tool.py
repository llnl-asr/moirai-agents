"""Analysis pipeline stage 1: take traces from the user and check what they hold."""
import os

from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGE = "ingest"


def register(mcp):
    @mcp.tool()
    def ingest_traces(pipeline: str) -> dict:
        """Read the traces the user gave, once, and report what they are. Use first in an analysis pipeline. Returns {files, bytes, counts, spill_files}."""
        c = P.load(pipeline)
        files = T.expand(c["traces"])
        size = sum(os.path.getsize(f) for f in files)
        n = T.read(c["traces"]).counts()
        spill = [os.path.basename(f) for f in files if "-spill" in f]
        s = f"{len(files)} file(s), {n['activities']} activities, {n['entities']} entities"
        P.record(pipeline, STAGE, status="running", outputs={"files": len(files), "bytes": size, "counts": n, "spill_files": spill, "summary": s}, note=s)
        return {"files": len(files), "bytes": size, "counts": n, "spill_files": spill[:20]}

    @mcp.tool()
    def ingest_validate(pipeline: str) -> dict:
        """Validate the ingest stage: trace files exist and hold a provenance graph with no dangling edge. Use after ingest_traces. Returns the verdict {status, failed, unknown, passed, total}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if v.check("files", bool(o.get("files")), "no trace file found", "ask the user for the path: a directory or a glob of .pfw or .pfw.gz files"):
            n = o["counts"]
            v.check("provenance", n["entities"] + n["activities"] > 0, str(n), "these traces have no moirai records: were they made without moirai?")
            v.check("no_dangling", n["dangling"] == 0, f"{n['dangling']} ids with no EH record", "some trace files are missing from this set?")
        return P.finish_validation(pipeline, v)
