"""Reading provenance traces: the one reader every graph, query, check and aspect uses (moirai_agents.core.provtrace on dftracer-utils'
TraceViewer, with its index). Nothing else in moirai-agents imports dftracer.utils or opens a trace file: the capability store refuses it."""
import tempfile

from moirai_agents.core import provtrace as T


def register(mcp):
    @mcp.tool()
    def prov_read_files(traces: str) -> dict:
        """The trace files a path names (a directory, a glob, a file, or several joined by ':'), with their sizes and the TraceViewer index they
        use (beside them when writable, else a per-user cache). Use to see what a pipeline's traces are before reading them.
        Returns {files: [{file, bytes}], index}."""
        files = T.expand(traces)
        import os
        return {"files": [{"file": f, "bytes": os.path.getsize(f)} for f in files if os.path.exists(f)], "index": T.index_dir(files) if files else ""}

    @mcp.tool()
    def prov_read_counts(traces: str) -> dict:
        """Row counts of the traces by category and name through TraceViewer group-bys (seconds, no record turned into Python): the provenance
        records (ET/EH/ER), the activities, and every other category (dftracer's I/O and system events). Use for a quick picture of what a trace
        holds. Returns {"<cat>:<name>": rows, files}."""
        return T.row_counts(traces)

    @mcp.tool()
    def prov_read_selftest() -> dict:
        """Self-check of the reader on a synthetic trace: the graph read through TraceViewer and the row counts must match what was written.
        Returns {status, got, line}."""
        from moirai_agents.core.aspects import _selftest as ST
        with tempfile.TemporaryDirectory(prefix="prov_read_") as d:
            ST.write(d)
            g = T.read(d).counts()
            rc = T.row_counts(d)
        want = {"activities": 2, "entities": 10, "relations": 1}
        ok = all(g.get(k) == v for k, v in want.items()) and rc.get("dftracer:EH") == 10 and rc.get("files") == 1
        return {"status": "ok" if ok else "error", "got": {k: g.get(k) for k in want} | {"EH rows": rc.get("dftracer:EH")},
                "line": "prov_read selftest " + ("PASS" if ok else "FAIL")}
