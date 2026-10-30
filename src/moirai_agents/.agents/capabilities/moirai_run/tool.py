"""Run pipeline stages 5 and 6: run the annotated app under moirai-service, first a smoke run, then the baseline, watched live on the Dashboard."""
import json
import shlex
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGES = ("moirai_smoke", "baseline")


def _script(env, pid, stage, log, data_dir, command, launcher, nodes):
    q = shlex.quote
    svc_all = f"flux run -N {nodes} --tasks-per-node=1 " if nodes > 1 else ""
    shm = f"/moirai-agents-{pid[-20:]}-{stage}".replace(".", "-")
    return f"""set -uo pipefail
source {q(env)}
export MOIRAI_LOG_FILE={q(log)}
export MOIRAI_DATA_DIR={q(data_dir)}
export MOIRAI_SERVICE_SHM={shm}
export MOIRAI_PC_BLOCKS=${{MOIRAI_PC_BLOCKS:-16}}
export MOIRAI_PC_FLUSH_MS=${{MOIRAI_PC_FLUSH_MS:-250}}   # how fresh the live view is
stop() {{ trap - EXIT TERM INT; {svc_all}moirai-service stop; }}
trap stop EXIT
trap 'exit 143' TERM INT
{svc_all}moirai-service start
for _ in $(seq 100); do [ -s "/dev/shm$MOIRAI_SERVICE_SHM" ] && break; sleep 0.1; done
[ -s "/dev/shm$MOIRAI_SERVICE_SHM" ] || echo "moirai-service ring not visible here: events may go to spill files" >&2
{launcher} {command}
"""


def _start(pipeline, stage, command, launcher, nodes, data_dir, wait, limit):
    c = P.load(pipeline)
    P.require_ready(c, stage)
    integ, cmd = P.stage(c, "integrate")["outputs"], command or c["command"]
    if not integ.get("env"):
        return {"status": "error", "error": "the integrate stage has not written moirai_env.sh"}
    tdir = P.pdir(pipeline) / "traces" / stage
    tdir.mkdir(parents=True, exist_ok=True)
    wd = integ["annotated"]
    script = _script(integ["env"], pipeline, stage, str(tdir / "prov"), data_dir or wd, cmd, launcher, nodes)
    j = J.start(pipeline, stage, stage.replace("_", "-"), script, wd, limit=limit)
    P.set_dashboard(pipeline, str(tdir), live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": str(tdir), "command": cmd, "launcher": launcher, "nodes": nodes}, note=f"running under moirai: {cmd}")
    r = J.wait(pipeline, j["id"], wait)
    return {"status": r["status"], "job": j["id"], "rc": r["rc"], "elapsed": r["elapsed"], "traces": str(tdir), "log_tail": r.get("log_tail", "")[-2500:],
            "note": "the Dashboard tab shows the provenance events as they arrive"}


def register(mcp):
    @mcp.tool()
    def moirai_smoke_run(pipeline: str, command: str = "", launcher: str = "", wait: int = 600, limit: int = 1800) -> dict:
        """Run the annotated app on the small test under moirai-service. Use after integrate. It stops the service however the app ends; limit: seconds before it is
        stopped. The Dashboard tab follows it live. Returns {status, job, rc, elapsed, traces, log_tail}."""
        return _start(pipeline, "moirai_smoke", command, launcher, 1, "", wait, limit)

    @mcp.tool()
    def moirai_baseline_run(pipeline: str, command: str, launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = 3600) -> dict:
        """Start the real baseline run under moirai-service, in the background, on what the user approved at the gate. command: the full run. launcher: e.g. 'flux run -N4 -n32'.
        nodes: one service starts per node. data_dir: where its I/O is traced. limit: seconds before it is stopped. The Dashboard tab shows events as they appear. Returns
        {status, job, traces}: watch with moirai_run_status."""
        return _start(pipeline, "baseline", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_run_status(pipeline: str, stage: str) -> dict:
        """Follow a run: job state, log tail and what the trace holds so far (readable while the run is going). Use while it runs, to report progress or spot a stall.
        Returns {job, rc, elapsed, counts, log_tail}."""
        o = P.stage(P.load(pipeline), stage)["outputs"]
        j = J.get(pipeline, o["job"], 2000)
        return {"job": j["id"], "status": j["status"], "rc": j["rc"], "elapsed": j["elapsed"], "timed_out": j.get("timed_out", False), "counts": T.read(o["traces"]).counts(), "log_tail": j["log_tail"]}

    @mcp.tool()
    def moirai_run_stop(pipeline: str, stage: str) -> dict:
        """Stop a run (the service is stopped and the trace closed). Use when the user asks, or the run is clearly stuck. Returns {status, id, stopped}."""
        return J.stop(pipeline, P.stage(P.load(pipeline), stage)["outputs"]["job"])

    @mcp.tool()
    def moirai_validate(pipeline: str, stage: str) -> dict:
        """Validate a moirai run: exit 0, a trace from the service and not spill files, activities and entities recorded, no dangling edge, and every entity type in
        the spec seen. stage: moirai_smoke|baseline. Use after the job ended. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        o = P.stage(c, stage)["outputs"]
        v = P.Verdict(stage)
        if not v.check("ran", bool(o.get("job")), "the run has not started", "start the run"):
            return P.finish_validation(pipeline, v)
        j = J.get(pipeline, o["job"], 1500)
        if j["status"] == "running":
            v.inconclusive("finished", f"still running after {j['elapsed']} s")
            return P.finish_validation(pipeline, v)
        v.check("exit_0", j["rc"] == 0, f"rc={j['rc']}{' (timed out)' if j.get('timed_out') else ''}: {j.get('log_tail', '')[-300:]}",
                "raise limit if it timed out; otherwise compare with the smoke stage: did the annotation break it?")
        files = T.expand(o["traces"])
        v.check("trace", bool(files), "no trace file", "read the service log next to the trace prefix")
        v.check("service_trace", not files or not all("-spill" in f for f in files), "only spill files: no service was running", "check the service log; is /dev/shm big enough?")
        g = T.read(o["traces"])
        n = g.counts()
        v.check("activities", n["activities"] > 0, f"{n['activities']} activities", "is the annotated code on the executed path? Is the app run from the annotated tree?")
        v.check("entities", n["entities"] > 0, f"{n['entities']} entities", "activities relate no entity: check the spec")
        v.check("no_dangling", n["dangling"] == 0, f"{n['dangling']} ids with no EH record", "every related entity must be declared")
        spec = P.stage(c, "annotate")["outputs"].get("spec") or {}
        seen = {e["type"] for e in g.entities.values()}
        missing = sorted({e["type"] for a in spec.get("activities", []) for e in a.get("used", []) + a.get("generated", [])} - seen)
        v.check("types_seen", not missing, f"never recorded: {missing}", "that code path did not run: use a test input that reaches it, or fix the spec")
        P.record(pipeline, stage, outputs={"counts": n, "summary": f"{n['activities']} activities, {n['entities']} entities, {n['edges']} edges"})
        return P.finish_validation(pipeline, v)
