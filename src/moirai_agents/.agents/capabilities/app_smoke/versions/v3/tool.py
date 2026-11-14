"""Run pipeline stage 2: build the ORIGINAL application and run the small test, so later failures can be pinned on what we changed."""
import os
import shlex
import time
from pathlib import Path

from moirai_agents.core import dfbridge as B
from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P

STAGE = "smoke"
SKIP = {".git", "__pycache__", "build", ".venv", "venv"}
RULE = "big builds go to compute nodes unless the user says so"
RC_KEYS = ("rc", "returncode", "return_code", "exit_code", "exitcode", "exit_status")
TAIL_KEYS = ("log_tail", "stderr", "stdout", "output", "error")
MAX_PATHS = 200


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


def _data_inventory(data_dir):
    """Pure-ish: everything under a user-named data directory. {status: ok, data_dir, files, bytes, by_suffix, paths} or {status: error, error}."""
    d = (data_dir or "").strip()
    if not d or not os.path.isdir(d):
        return {"status": "error", "error": f"data_dir {d!r} does not exist or is not a directory: pass the directory the user named, quoting their answer"}
    files, total, by, paths = 0, 0, {}, []
    for dp, dn, fn in os.walk(d):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            try:
                size = os.stat(p).st_size
            except OSError:
                continue
            files += 1
            total += size
            suf = Path(f).suffix.lower() or "(no suffix)"
            by[suf] = by.get(suf, 0) + 1
            if len(paths) < MAX_PATHS:
                paths.append(p)
    return {"status": "ok", "data_dir": d, "files": files, "bytes": total, "by_suffix": by, "paths": paths}


def _suffix_text(by):
    return ", ".join(f"{n} {s}" for s, n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])))


def _data_summary(inv):
    if not inv.get("files"):
        return "data_dir is empty"
    return f"data_dir has {_suffix_text(inv.get('by_suffix') or {})} files, {inv['bytes']} bytes"


def _rc_of(step):
    """The exit code a dftracer step reports (top level or under 'result'), or None when it reports none."""
    for d in (step, step.get("result") if isinstance(step.get("result"), dict) else {}):
        for k in RC_KEYS:
            v = d.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                return int(v.strip())
    return None


def _tail_of(step, lines=10):
    for d in (step, step.get("result") if isinstance(step.get("result"), dict) else {}):
        for k in TAIL_KEYS:
            if isinstance(d.get(k), str) and d[k].strip():
                return "\n".join(d[k].rstrip().splitlines()[-lines:])
    return ""


def _faults(steps):
    """Every step that did not succeed: a bad status or a non-zero exit code. [{step, rc, tail}]"""
    out = []
    for n, st in (steps or {}).items():
        rc = _rc_of(st) if isinstance(st, dict) else None
        if not isinstance(st, dict) or not B.ok(st) or (rc not in (None, 0)):
            out.append({"step": n, "rc": rc, "tail": _tail_of(st) if isinstance(st, dict) else ""})
    return out


def _fault_detail(faults):
    s = "; ".join(f"step {f['step']} rc={f['rc']}: {f['tail']}" for f in faults)
    return s[:395] if len(s) <= 395 else "..." + s[-392:]      # a verdict keeps 400 chars: keep the END of the log


def _plan(build_command, command, launcher, build_where, approval):
    """Pure: the job script and header for smoke_run, or the error that refuses it."""
    if build_where not in ("compute", "login"):
        return {"status": "error", "error": f"build_where must be 'compute' or 'login', got {build_where!r}"}
    approval = (approval or "").strip()
    if build_where == "compute" and approval:
        return {"status": "error", "error": "user_approved_login_build is only for build_where='login'; drop it or choose login"}
    head = [f"# build_where={build_where}"]
    if build_where == "login":
        if not approval or "login" not in approval.lower():
            return {"status": "error", "error": f"build_where='login' needs user_approved_login_build quoting the user's own words about building on the login node. Rule: {RULE}. "
                                                "Ask the user (ask_user) and pass what they said."}
        head.append(f"# user_approved_login_build={approval}")
        if not launcher:
            return {"status": "error", "error": "build_where='login' still needs a launcher: the smoke run itself stays on a compute node (e.g. 'flux run -N1')"}
    elif build_command and not launcher:
        return {"status": "error", "error": f"build_where='compute' (default) needs a launcher for the build (e.g. 'flux run -N1'). Rule: {RULE}."}
    lines = ["set -e"] + [f"echo {shlex.quote(h)}" for h in head]
    if build_command:
        lines.append(build_command if build_where == "login" else f"{launcher} bash -c {shlex.quote(build_command)}")
    lines.append(f"{launcher} {command}".strip())
    return {"status": "ok", "script": "\n".join(lines) + "\n", "header": head}


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
        bad = _faults({name: steps[name]})
        if bad:
            P.record(pipeline, STAGE, status="failed", outputs={"session_steps": steps, "summary": f"smoke FAILED at {name}: {_fault_detail(bad)}"}, note=f"{name} failed")
            return {"status": "error", "failed_step": name, "run_id": rid, "steps": steps, "detail": _fault_detail(bad)}
    new = sorted(k for k, m in _inventory(wd).items() if m[1] >= started - 1 and k not in before or before.get(k, (0, 0))[1] != m[1])
    P.record(pipeline, STAGE, outputs={"session_steps": steps, "command": cmd, "cwd": wd, "outputs_created": new[:200], "summary": f"session {rid}: smoke passed (every step rc 0), wrote {len(new)} file(s)"})
    return {"status": "ok", "run_id": rid, "steps": steps, "outputs_created": new[:50]}


def register(mcp):
    @mcp.tool()
    def smoke_rc_verdict(steps: dict) -> dict:
        """Judge session steps by exit code as well as status: any step with a non-zero rc (or a failed status) is a fault. Use inside smoke_validate, or to re-check
        recorded steps. steps: {step_name: {status, rc|returncode|exit_code, log_tail|stderr|stdout}}. Returns {status: pass|fail, faults: [{step, rc, tail}], detail, fix}."""
        f = _faults(steps)
        if not f:
            return {"status": "pass", "faults": []}
        return {"status": "fail", "faults": f, "detail": _fault_detail(f),
                "fix": "read the log tail, fix the build or command (never the source), re-run; a file-creation error under python3.N/site-packages on a compute node is the site rule in the skill"}

    @mcp.tool()
    def smoke_data_inventory(data_dir: str) -> dict:
        """List the data a smoke run wrote into a directory the user named (outside the session). Use when smoke_validate says 'ask the user where its data goes' and they
        answered, or to look before passing data_dir. data_dir: absolute path of that directory. Returns {status: ok, data_dir, files, bytes, by_suffix: {suffix: count},
        paths (first 200)} or {status: error, error} when the directory does not exist."""
        return _data_inventory(data_dir)

    @mcp.tool()
    def smoke_data_verdict(inventory: dict) -> dict:
        """Judge a data_dir inventory (the output of smoke_data_inventory) as the 'outputs' check: pass when it holds at least one file, inconclusive when it is empty or absent
        (an empty data_dir is not proof the run wrote data). Returns {status: pass|inconclusive, summary, detail}."""
        if not isinstance(inventory, dict) or inventory.get("status") == "error" or not inventory.get("files"):
            return {"status": "inconclusive", "summary": "data_dir is empty", "detail": "the data_dir holds no file: ask the user where the run really wrote its data"}
        return {"status": "pass", "summary": _data_summary(inventory), "detail": ""}

    @mcp.tool()
    def smoke_build_plan(command: str, build_command: str = "", launcher: str = "", build_where: str = "compute", user_approved_login_build: str = "") -> dict:
        """Dry-run of smoke_run's placement rule: where the environment build runs and the job script it would write, without starting anything. Use to check an exception before
        asking. build_where: 'compute' (default; the build goes through launcher) or 'login' (ONLY with user_approved_login_build, the user's own words; rule: big builds go to
        compute nodes unless the user says so). Returns {status, script, header} or {status: error, error}."""
        return _plan(build_command, command, launcher, build_where, user_approved_login_build)

    @mcp.tool()
    def smoke_session(pipeline: str, command: str = "", build: bool = True, extra_cmake_flags: str = "", subfolder: str = "source", timeout: int = 900) -> dict:
        """Build and smoke-test the original app in the pipeline's dftracer session. Use when pipeline_status shows a session. build=false for an app with nothing to build, such as a
        plain script. subfolder: where the command runs, inside the session. Fails on any step with a non-zero rc. Returns {status, run_id, steps, outputs_created} or {status: error, failed_step, steps, detail}."""
        c = P.load(pipeline)
        if not c.get("session"):
            return {"status": "error", "error": "this pipeline has no session: use smoke_run"}
        return _session_smoke(pipeline, c, command or c["command"], build, extra_cmake_flags, subfolder, timeout)

    @mcp.tool()
    def smoke_run(pipeline: str, build_command: str = "", command: str = "", cwd: str = "", launcher: str = "", wait: int = 600,
                  build_where: str = "compute", user_approved_login_build: str = "", data_dir: str = "") -> dict:
        """Build and run the original app on the small test, in one background job (jobs.start). Use after explore. build_command: skip if none. command: override the
        pipeline's test command. cwd: where it runs (default the app). launcher: e.g. 'flux run -q pdebug' for a compute node; required for a build. build_where: 'compute'
        (default: the build goes through launcher) or 'login' (the build runs bare on the login node, the smoke run still goes through launcher); 'login' is accepted ONLY with
        user_approved_login_build, the user's own words, because big builds go to compute nodes unless the user says so; both are written in the job log header. data_dir: optional
        absolute directory where the app writes its data when that is outside cwd (must exist); it is recorded in the stage outputs and smoke_validate reuses it. wait: seconds
        to wait (a longer run keeps going; check with job_status, then smoke_validate). Returns {job, status, rc, log_tail} or {status: error, error}."""
        c = P.load(pipeline)
        cmd = command or c["command"]
        wd = cwd or P.source_of(c)
        plan = _plan(build_command, cmd, launcher, build_where, user_approved_login_build)
        if plan["status"] != "ok":
            return plan
        if data_dir.strip() and not os.path.isdir(data_dir.strip()):
            return _data_inventory(data_dir)
        before = _inventory(wd)
        j = J.start(pipeline, STAGE, "smoke", plan["script"], wd)
        P.record(pipeline, STAGE, status="running", outputs={"job": j["id"], "command": cmd, "build_command": build_command, "cwd": wd, "launcher": launcher, "build_where": build_where,
                                                              "user_approved_login_build": user_approved_login_build.strip(), "data_dir": data_dir.strip(),
                                                              "before": {k: v[0] for k, v in before.items()}, "started": time.time()}, note=f"running ({build_where} build): {cmd}")
        r = J.wait(pipeline, j["id"], wait)
        return {"job": j["id"], "status": r["status"], "rc": r["rc"], "elapsed": r["elapsed"], "log_tail": r.get("log_tail", "")[-3000:]}

    @mcp.tool()
    def smoke_validate(pipeline: str, data_dir: str = "") -> dict:
        """Validate the smoke stage: every step or the job finished with exit code 0 (a non-zero rc fails with the last 10 log lines), and list what it wrote (those files are the
        candidate data for annotation). For a job submitted by flux batch the job rc is the launcher script's rc (it exits non-zero when the batch job's return code is non-zero), so
        rc 0 is trusted and non-zero fails. Use after smoke_run or smoke_session. data_dir: optional absolute directory the user named as where the data goes when the run wrote
        outside its cwd/session (quote their answer); default the data_dir recorded by smoke_run. It must exist (else {status: error}); its files, bytes, count by suffix and first
        200 paths go to outputs_created and the summary; non-empty passes the 'outputs' check, empty stays inconclusive. Returns the verdict, or {status: error, error}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        dd = (data_dir or o.get("data_dir") or "").strip()
        v = P.Verdict(STAGE)

        def data_outputs(extra):
            inv = _data_inventory(dd)
            if inv["status"] != "ok":
                return inv, False
            summ = f"{extra}, {_data_summary(inv)}"
            P.record(pipeline, STAGE, outputs={"data_dir": dd, "outputs_created": inv["paths"], "data_files": inv["files"], "data_bytes": inv["bytes"], "data_by_suffix": inv["by_suffix"], "summary": summ})
            if inv["files"]:
                return inv, True
            v.inconclusive("outputs", f"data_dir {dd} is empty: ask the user where the run wrote its data")
            return inv, True

        if o.get("session_steps"):
            f = _faults(o["session_steps"])
            v.check("session_steps", not f, _fault_detail(f), "fix the failed step (see its rc and log tail) and re-run smoke_session")
            if not f:
                if dd:
                    inv, good = data_outputs("every step rc 0")
                    if not good:
                        return inv
                elif not o.get("outputs_created"):
                    v.inconclusive("outputs", "the run wrote no file in the session: ask the user where its data goes, then call smoke_validate with data_dir")
        elif v.check("ran", bool(o.get("job")), "smoke_run has not run", "run smoke_session or smoke_run"):
            j = J.get(pipeline, o["job"], 1500)
            if j["status"] == "running":
                v.inconclusive("finished", "still running")
            else:
                tail = "\n".join(j.get("log_tail", "").rstrip().splitlines()[-10:])
                detail = f"rc={j['rc']}: {tail}"
                ok = v.check("exit_0", j["rc"] == 0, detail if len(detail) <= 395 else "..." + detail[-392:],
                             "fix the build or command; if the log says creating a file under python3.N/site-packages failed on a compute node, apply the site rule in the skill (build_where); "
                             "for a flux batch job this rc is the launcher script's rc, so read the batch job's own output")
                if not ok:
                    P.record(pipeline, STAGE, outputs={"summary": f"smoke FAILED rc={j['rc']}", "failure_log_tail": tail})
                if ok:
                    if dd:
                        inv, good = data_outputs(f"exit 0 in {j['elapsed']}s")
                        if not good:
                            return inv
                        P.record(pipeline, STAGE, outputs={"seconds": j["elapsed"]})
                    else:
                        now = _inventory(o["cwd"])
                        new = sorted(k for k, m in now.items() if m[1] >= o["started"] - 1)
                        P.record(pipeline, STAGE, outputs={"outputs_created": new[:200], "seconds": j["elapsed"], "summary": f"exit 0 in {j['elapsed']}s, wrote {len(new)} file(s)"})
                        if not new:
                            v.inconclusive("outputs", "the run wrote no file in its directory: ask the user where its data goes, then call smoke_validate with data_dir")
        return P.finish_validation(pipeline, v)
