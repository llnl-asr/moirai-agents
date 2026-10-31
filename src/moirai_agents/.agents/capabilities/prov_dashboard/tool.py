"""The Dashboard: dfdashboard, watching the pipelines tree behind the web UI's port. Point the tab at a run; traces from elsewhere are linked into the tree."""
import os

from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGE = "dashboard"


def register(mcp):
    @mcp.tool()
    def dashboard_open(pipeline: str, traces: str = "", live: bool = False) -> dict:
        """Show traces on the web UI's Dashboard tab. live=true for a run still writing them: new events are pushed as they appear. traces: a directory or glob; default the
        pipeline's. moirai runs do this by themselves. Returns {status, traces, live, files}."""
        c = P.load(pipeline)
        t = traces or c.get("traces") or (c.get("dashboard") or {}).get("traces", "")
        files = T.expand(t) if t else []
        if not files and not live:
            return {"status": "error", "error": f"no trace files under {t!r}"}
        root = str(P.dash_root()) + os.sep
        if not os.path.abspath(t).startswith(root) or os.path.basename(t.rstrip("/")) != "raw":      # dfdashboard reads <root>/<run>/raw: link the user's traces there
            raw = P.pdir(pipeline) / "traces" / "analysis" / "raw"
            P.link_traces(t, raw)
            t = str(raw)
        P.set_dashboard(pipeline, t, live)
        if STAGE in c["stages"]:
            P.record(pipeline, STAGE, outputs={"traces": t, "live": live, "files": len(files), "opened": True}, note="dashboard open")
        return {"status": "ok", "traces": t, "live": live, "files": len(files)}

    @mcp.tool()
    def dashboard_validate(pipeline: str) -> dict:
        """Validate the dashboard stage: the dashboard is open on readable traces that hold a provenance graph. Use before the gate of an analysis pipeline.
        Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        v = P.Verdict(STAGE)
        d = c.get("dashboard") or {}
        if v.check("open", bool(d.get("traces")), "the dashboard was not opened", "run dashboard_open"):
            n = T.read(d["traces"]).counts()
            v.check("graph", n["entities"] + n["activities"] > 0, str(n), "these traces hold no moirai records")
            P.record(pipeline, STAGE, outputs={"counts": n, "summary": f"{n['activities']} activities, {n['entities']} entities on the Dashboard"})
        return P.finish_validation(pipeline, v)
