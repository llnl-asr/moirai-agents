"""Run pipeline stage 2: build the ORIGINAL application and run the small test, so later failures can be pinned on what we changed."""
import os
import time
from pathlib import Path

from moirai_agents.core import dfbridge as B
from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P

STAGE = "smoke"
SKIP = {".git", "__pycache__", "build", ".venv", "venv"}


def _inventory(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP]
        for f in fn:
            p = Path(dp) / f
            try:
                st = p.stat()
                out[str(p.relative_to(root))] = (st.st_size, st.st_mtime)
            except OSError:
                continue
    return out


def _session_smoke(pipeline, c, cmd, build, flags, subfolder, timeout):
    """Detect, configure, build and smoke-test in the pipeline's session, through dftracer-agents' tools, as its own pipeline does."""
    rid, wd = c["session"]["run_id"], P.source_of(c)
    plan = [("detect", "session_detect", {"run_id": rid})]
    if build:
        plan += [("configure", "session_configure", {"run_id": rid, "extra_cmake_flags": flags}), ("build", "session_build_install", {"run_id": rid})]
    plan += [("smoke", "session_run_smoke_test", {"run_id": rid, "command": cmd, "subfolder": subfolder, "timeout": timeout})]
    before, started, steps = _inventory(wd), time.time(), {}
    for name, tool, args in plan:
        P.record(pipeline, STAGE, status="running", note=f"session: {name}")
        steps[name] = B.call(tool, args)
        if not B.ok(steps[name]):
            P.record(pipeline, STAGE, status="failed", outputs={"session_steps": steps}, note=f"{name} failed")
            return {"status": "error", "failed_step": name, "run_id": rid, "steps": steps}
    new = sorted(k for k, m in _inventory(wd).items() if m[1] >= started - 1 and k not in before or before.get(k, (0, 0))[1] != m[1])
    P.record(pipeline, STAGE, outputs={"session_steps": steps, "command": cmd, "cwd": wd, "outputs_created": new[:200], "summary": f"session {rid}: smoke passed, wrote {len(new)} file(s)"})
    return {"status": "ok", "run_id": rid, "steps": steps, "outputs_created": new[:50]}


def register(mcp):
    @mcp.tool()
    def smoke_session(pipeline: str, command: str = "", build: bool = True, extra_cmake_flags: str = "", subfolder: str = "source", timeout: int = 900) -> dict:
        """Build and smoke-test the original app in the pipeline's dftracer session. Use when pipeline_status shows a session. build=false for an app with nothing to build, such as a
        plain script. subfolder: where the command runs, inside the session. Returns {status, run_id, steps, outputs_created} or {status: error, failed_step, steps}."""
        c = P.load(pipeline)
        if not c.get("session"):
            return {"status": "error", "error": "this pipeline has no session: use smoke_run"}
        return _session_smoke(pipeline, c, command or c["command"], build, extra_cmake_flags, subfolder, timeout)

    @mcp.tool()
    def smoke_run(pipeline: str, build_command: str = "", command: str = "", cwd: str = "", launcher: str = "", wait: int = 600) -> dict:
        """Build and run the original app on the small test, in the background. Use after explore. build_command: skip if none. command: override the
        pipeline's test command. cwd: where it runs (default the app). launcher: e.g. 'flux run -N1' for a compute node. wait: seconds to wait
        (a longer run keeps going; check with job_status, then smoke_validate). Returns {job, status, rc, log_tail}."""
        c = P.load(pipeline)
        cmd = command or c["command"]
        wd = cwd or P.source_of(c)
        script = "set -e\n" + (f"{build_command}\n" if build_command else "") + f"{launcher} {cmd}\n".lstrip()
        before = _inventory(wd)
        j = J.start(pipeline, STAGE, "smoke", script, wd)
        P.record(pipeline, STAGE, status="running", outputs={"job": j["id"], "command": cmd, "build_command": build_command, "cwd": wd, "launcher": launcher,
                                                              "before": {k: v[0] for k, v in before.items()}, "started": time.time()}, note=f"running: {cmd}")
        r = J.wait(pipeline, j["id"], wait)
        return {"job": j["id"], "status": r["status"], "rc": r["rc"], "elapsed": r["elapsed"], "log_tail": r.get("log_tail", "")[-3000:]}

    @mcp.tool()
    def smoke_validate(pipeline: str) -> dict:
        """Validate the smoke stage: the job finished with exit 0, and list what it wrote (those files are the candidate data for annotation).
        Use after smoke_run. Returns the verdict; the new files go to the stage outputs as 'outputs_created'."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if o.get("session_steps"):
            bad = [n for n, st in o["session_steps"].items() if not B.ok(st)]
            v.check("session_steps", not bad, f"failed: {bad}", "fix the failed step and re-run smoke_session")
            if not o.get("outputs_created"):
                v.inconclusive("outputs", "the run wrote no file in the session: ask the user where its data goes")
        elif v.check("ran", bool(o.get("job")), "smoke_run has not run", "run smoke_session or smoke_run"):
            j = J.get(pipeline, o["job"], 1500)
            if j["status"] == "running":
                v.inconclusive("finished", "still running")
            else:
                ok = v.check("exit_0", j["rc"] == 0, f"rc={j['rc']}: {j.get('log_tail', '')[-300:]}", "fix the build or command; the log is in job_status")
                if ok:
                    now = _inventory(o["cwd"])
                    new = sorted(k for k, m in now.items() if m[1] >= o["started"] - 1)
                    P.record(pipeline, STAGE, outputs={"outputs_created": new[:200], "seconds": j["elapsed"], "summary": f"exit 0 in {j['elapsed']}s, wrote {len(new)} file(s)"})
                    if not new:
                        v.inconclusive("outputs", "the run wrote no file in its directory: ask the user where its data goes")
        return P.finish_validation(pipeline, v)
