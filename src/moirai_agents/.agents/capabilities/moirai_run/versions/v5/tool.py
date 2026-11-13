"""Run pipeline stages 5 and 6: run the annotated app under moirai-service, first a smoke run, then the baseline, watched live on the Dashboard.
Two shapes: a plain command (one launcher), or a generated `flux batch` run.sh (batch mode): run.sh stays UNMODIFIED; its app job runs a generated
per-chunk submit wrapper (--submit-script) that starts moirai-service inside the job's own shell."""
import json
import math
import os
import re
import shlex
import shutil
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGES = ("moirai_smoke", "baseline")
MARKER = "moirai preamble (generated)"
DFT_REL = "../../dftracer/bin/dftracer_service"
WRAPPER_NAME = "submit_moirai.sh"
ORIGINAL_REL = "monomer-structure-prediction/submit_smsp.sh"   # Elmerfold's per-chunk submit script, under ELMER_SRC_DIR


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


# ---------------------------------------------------------------- dftracer_service resolver
def resolve_dftracer_service(binary: str = "", venv: str = "") -> dict:
    """Where moirai-service's embedded relative path ../../dftracer/bin/dftracer_service points, and whether it exists.
    Candidates: relative to the real path of the binary (only when the binary contains that string), then <venv>/lib/python*/site-packages/dftracer/bin."""
    b = binary or shutil.which("moirai-service") or ""
    tried, found, embedded = [], "", False
    if b and os.path.isfile(b):
        real = os.path.realpath(b)
        try:
            embedded = DFT_REL.encode() in Path(real).read_bytes()
        except OSError:
            embedded = False
        if embedded:
            tried.append(os.path.normpath(os.path.join(os.path.dirname(real), DFT_REL)))
    if venv:
        import glob
        tried += sorted(glob.glob(os.path.join(os.path.expanduser(venv), "lib", "python*", "site-packages", "dftracer", "bin", "dftracer_service")))
    for c in tried:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            found = os.path.realpath(c)
            break
    return {"binary": b, "embedded_relative_path": embedded, "tried": tried, "path": found}


# ---------------------------------------------------------------- the per-chunk submit wrapper (pure text)
_SUBMIT = r'''#!/bin/bash
# __MARK__: per-chunk submit wrapper. flux bulksubmit runs it on the exclusive worker node as: submit_moirai.sh <chunk csv>
# It starts moirai-service in the background of THIS shell (never via flux run/submit), runs the original submit script as a child,
# and stops the service with the same environment. Arguments are passed to the original unchanged.
ORIG=__ORIG__
[ -f "${ORIG}" ] || { echo "moirai-wrapper: original submit script ${ORIG} not found" >&2; exit 2; }
[ -f __ENV__ ] || { echo "moirai-wrapper: environment file __ENV__ not found" >&2; exit 2; }
source __ENV__ || { echo "moirai-wrapper: sourcing __ENV__ failed" >&2; exit 2; }
command -v moirai-service >/dev/null 2>&1 || { echo "moirai-wrapper: moirai-service not on PATH after sourcing __ENV__" >&2; exit 2; }
_job="${FLUX_JOB_ID:-$$}"
_host="$(hostname)"
export MOIRAI_LOG_FILE=__LOG__
export MOIRAI_DATA_DIR=__DATA__
export MOIRAI_SERVICE_SHM="__SHM__-${_job}"
export MOIRAI_PC_BLOCKS=${MOIRAI_PC_BLOCKS:-16}
export MOIRAI_PC_FLUSH_MS=${MOIRAI_PC_FLUSH_MS:-250}
# moirai-service --help prints a build-time default for dftracer_service; when it does not exist, point to the installed one
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  _dflt="$(moirai-service --help 2>&1 | grep -o '/[^[:space:]]*dftracer_service' | head -1)"
  if [ -z "${_dflt}" ] || [ ! -x "${_dflt}" ]; then
    _real="$(readlink -f "$(command -v moirai-service)")"
    for _c in __RESOLVED__"$(dirname "${_real}")/../../dftracer/bin/dftracer_service"__VENVGLOB__; do
      if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
    done
    [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service found (default ${_dflt:-unknown} missing)" >&2; exit 2; }
  fi
fi
_svclog=__LOGDIR__/service-${_host}-${_job}.log
echo "${_host}" >> __HOSTS__
echo "moirai-wrapper: job ${_job} on ${_host}: starting moirai-service (ring /dev/shm${MOIRAI_SERVICE_SHM}, log ${_svclog})"
moirai-service start >"${_svclog}" 2>&1 &
_svc=$!
_stopped=0
_child=""
_stop() {
  [ "${_stopped}" = 1 ] && return 0
  _stopped=1
  moirai-service stop >>"${_svclog}" 2>&1 || echo "moirai-wrapper: moirai-service stop failed (see ${_svclog})" >&2
  for _i in $(seq 1 50); do kill -0 "${_svc}" 2>/dev/null || break; sleep 0.2; done
  if kill -0 "${_svc}" 2>/dev/null; then kill "${_svc}" 2>/dev/null; fi
  return 0
}
_sig() { if [ -n "${_child}" ]; then kill -TERM "${_child}" 2>/dev/null; wait "${_child}" 2>/dev/null; fi; exit "$1"; }
trap '_stop' EXIT
trap '_sig 143' TERM
trap '_sig 130' INT
for _i in $(seq 1 __WAIT10__); do [ -s "/dev/shm${MOIRAI_SERVICE_SHM}" ] && break; sleep 0.1; done
if [ ! -s "/dev/shm${MOIRAI_SERVICE_SHM}" ]; then
  echo "moirai-wrapper: ring /dev/shm${MOIRAI_SERVICE_SHM} not visible on ${_host} after __WAIT__ s; not running the app (service log ${_svclog})" >&2
  tail -n 20 "${_svclog}" >&2
  exit 3
fi
bash "${ORIG}" "$@" &
_child=$!
wait "${_child}"
rc=$?
_child=""
_stop
exit ${rc}
'''


def render_submit_wrapper(original, env, stage_dir, data_dir="", shm_base="/moirai-agents-run", wait_s=60, venv="", dft_service="") -> str:
    """Pure: the text of <stage_dir>/submit_moirai.sh. Paths are inserted shell-quoted; nothing is read or run."""
    q = shlex.quote
    sd = str(stage_dir).rstrip("/")
    shm = re.sub(r"[^A-Za-z0-9_/.-]", "-", shm_base).replace(".", "-")
    venvglob = f" {q(venv)}/lib/python*/site-packages/dftracer/bin/dftracer_service" if venv else ""
    resolved = f"{q(dft_service)} " if dft_service else ""
    t = (_SUBMIT.replace("__MARK__", "moirai-agents generated").replace("__ORIG__", q(original)).replace("__ENV__", q(env))
         .replace("__LOG__", q(sd + "/raw/prov")).replace("__DATA__", q(data_dir or "/"))
         .replace("__SHM__", shm).replace("__RESOLVED__", resolved).replace("__VENVGLOB__", venvglob)
         .replace("__LOGDIR__", q(sd)).replace("__HOSTS__", q(sd + "/hosts.txt"))
         .replace("__WAIT10__", str(int(wait_s) * 10)).replace("__WAIT__", str(int(wait_s))))
    return t


def check_run_sh(run_sh: str, wrapper: str, annotated: str = "") -> dict:
    """Pure: does the generated run.sh run the wrapper per chunk, from the annotated tree, unmodified? Returns {ok, problems, bulksubmit, script, inputs}."""
    problems, script, inputs, line = [], "", [], ""
    if MARKER in run_sh:
        problems.append("run.sh holds an old moirai preamble (v3 design): regenerate it with elmerfold_run_script, do not rewrite it")
    bl = [l for l in run_sh.split("\n") if "flux bulksubmit" in l and not l.lstrip().startswith("#")]
    if len(bl) != 1:
        problems.append(f"expected exactly one flux bulksubmit line in run.sh, found {len(bl)}")
    else:
        line = bl[0].strip()
        try:
            toks = shlex.split(line)
        except ValueError as e:
            toks = []
            problems.append(f"cannot parse the bulksubmit line: {e}")
        if toks and ":::" not in toks:
            problems.append("the bulksubmit line has no ':::' input list: nothing would run")
        elif toks:
            i = toks.index(":::")
            inputs = toks[i + 1:]
            before = toks[:i]
            script = before[-2] if len(before) >= 2 and before[-1] == "{}" else (before[-1] if before else "")
            if not inputs:
                problems.append("the bulksubmit line has ':::' but no input after it")
            if script != wrapper:
                problems.append(f"run.sh does not reference the wrapper: the bulksubmit line runs {script or '<nothing>'}, expected {wrapper}; "
                                f"regenerate with elmerfold_run_script --submit-script {wrapper}")
    if annotated and annotated not in run_sh:
        problems.append(f"run.sh does not point into the annotated tree {annotated}: it was generated from another tree; regenerate from the annotated copy")
    return {"ok": not problems, "problems": problems, "bulksubmit": line, "script": script, "inputs": inputs}


def _wrapper(run_script, minutes, queue, modules, name):
    q = shlex.quote
    mods = " ".join(q(m) for m in modules)
    return f"""set -uo pipefail
if [ -n {q(mods)} ]; then
  type module >/dev/null 2>&1 || {{ echo "wrapper: the 'module' command is not defined in this shell; cannot load {mods}" >&2; exit 1; }}
  module load {mods} || {{ echo "wrapper: module load {mods} failed" >&2; exit 1; }}
fi
command -v dbcast >/dev/null 2>&1 || {{ echo "wrapper: dbcast not on PATH after module load; flux batch would propagate a shell without it" >&2; exit 1; }}
jid="$(flux batch -t {minutes} --queue {q(queue)} {q(run_script)})" || {{ echo "wrapper: flux batch submission failed" >&2; exit 1; }}
echo "batch job: $jid"
cleanup() {{ trap - TERM INT; flux cancel "$jid" 2>/dev/null; exit 143; }}
trap cleanup TERM INT
flux watch "$jid" || true
rc="$(flux jobs -n -o '{{returncode}}' "$jid" 2>/dev/null | tr -d ' ')"
res="$(flux jobs -n -o '{{result}}' "$jid" 2>/dev/null | tr -d ' ')"
echo "batch job $jid result=$res returncode=$rc"
if [ "$res" != "COMPLETED" ] || [ "$rc" != "0" ]; then
  echo "wrapper: batch job $jid FAILED (result=$res returncode=$rc); see its --output file, the chunk logs and the service logs next to the traces" >&2
  case "$rc" in ''|*[!0-9]*) exit 1 ;; 0) exit 1 ;; *) exit "$rc" ;; esac
fi
exit 0
"""


def render_batch(original, env, stage_dir, data_dir="", shm_base="/moirai-agents-run", queue="pdebug", limit=2400, run_sh="", run_script="",
                 annotated="", venv="", dft_service="", modules=("mpifileutils/0.11.1",), wait_s=60, stage="moirai_smoke") -> dict:
    """Pure: wrapper text, batch wrapper text, exact submit command, and (when run_sh text is given) the run.sh check. Never touches the cluster."""
    sd = str(stage_dir).rstrip("/")
    wpath = f"{sd}/{WRAPPER_NAME}"
    minutes = max(1, math.ceil(int(limit) / 60))
    rs = run_script or f"{str(Path(sd).parent)}/run.sh"
    submit = f"flux batch -t {minutes} --queue {shlex.quote(queue)} {shlex.quote(rs)}"
    r = {"status": "ok", "wrapper_path": wpath, "wrapper_script": render_submit_wrapper(original, env, sd, data_dir, shm_base, wait_s, venv, dft_service),
         "batch_wrapper": _wrapper(rs, minutes, queue, list(modules), stage), "submit_command": submit, "minutes": minutes, "run_script_path": rs,
         "original_script": original, "hosts_file": f"{sd}/hosts.txt", "traces": f"{sd}/raw", "warnings": []}
    if not venv and not dft_service:
        r["warnings"].append("no service venv given and dftracer_service not resolved here: the wrapper looks it up relative to the moirai-service binary only")
    if run_sh:
        chk = check_run_sh(run_sh, wpath, annotated)
        r["run_sh_check"] = chk
        if not chk["ok"]:
            r["status"] = "error"
            r["error"] = "run.sh check failed: " + "; ".join(chk["problems"])
    return r


def _plan(pipeline, stage, batch_script, queue, limit, data_dir, venv, original=""):
    c = P.load(pipeline)
    P.require_ready(c, stage)
    integ = P.stage(c, "integrate")["outputs"]
    if not integ.get("env"):
        raise P.PipelineError("the integrate stage has not written moirai_env.sh")
    sdir = P.work(c) / "traces" / stage
    shm = f"/moirai-agents-{pipeline[-20:]}-{stage}"
    run_sh, rs = "", ""
    if batch_script:
        src = Path(batch_script).expanduser()
        if not src.is_file():
            raise P.PipelineError(f"batch_script {batch_script} does not exist")
        run_sh, rs = src.read_text(), str(src)
    if not original:
        m = re.search(r"^\s*(?:export\s+)?ELMER_SRC_DIR=(\S+)", run_sh, re.M)
        if m:
            original = os.path.join(m.group(1).strip("'\""), ORIGINAL_REL)
        else:
            raise P.PipelineError("cannot tell the original submit script: give original_script (or a run.sh that exports ELMER_SRC_DIR)")
    if not Path(original).is_file():
        raise P.PipelineError(f"original submit script {original} does not exist")
    res = resolve_dftracer_service("", venv)
    r = render_batch(original, integ["env"], str(sdir), data_dir or integ["annotated"], shm, queue, limit, run_sh, rs, integ["annotated"], venv, res["path"], stage=stage)
    r["resolver"] = res
    r["stage_dir"], r["annotated"] = str(sdir), integ["annotated"]
    return c, r


def _start_batch(pipeline, stage, batch_script, queue, limit, data_dir, venv, wait, original):
    if not batch_script:
        raise P.PipelineError("give batch_script (the generated run.sh)")
    c, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, venv, original)
    if r["status"] != "ok":
        raise P.PipelineError(f"nothing was started: {r['error']}")
    Path(r["traces"]).mkdir(parents=True, exist_ok=True)
    wp = Path(r["wrapper_path"])
    wp.write_text(r["wrapper_script"])
    wp.chmod(0o755)
    Path(r["hosts_file"]).unlink(missing_ok=True)
    j = J.start(pipeline, stage, stage.replace("_", "-") + "-batch", r["batch_wrapper"], r["annotated"], limit=int(limit) + 300)
    P.set_dashboard(pipeline, r["traces"], live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": r["traces"], "batch_script": str(batch_script), "run_script": r["run_script_path"],
                                                        "wrapper": r["wrapper_path"], "hosts_file": r["hosts_file"], "submit_command": r["submit_command"], "queue": queue, "mode": "batch"},
             note=f"submitted as a flux batch: {r['submit_command']}")
    w = J.wait(pipeline, j["id"], wait)
    return {"status": w["status"], "job": j["id"], "rc": w["rc"], "elapsed": w["elapsed"], "traces": r["traces"], "submit_command": r["submit_command"],
            "log_tail": w.get("log_tail", "")[-2500:], "note": "the Dashboard tab shows the provenance as it arrives"}


def _start(pipeline, stage, command, launcher, nodes, data_dir, wait, limit):
    c = P.load(pipeline)
    P.require_ready(c, stage)
    integ, cmd = P.stage(c, "integrate")["outputs"], command or c["command"]
    if not integ.get("env"):
        return {"status": "error", "error": "the integrate stage has not written moirai_env.sh"}
    tdir = P.work(c) / "traces" / stage / "raw"     # dfdashboard's run layout: <root>/<run>/raw/*.pfw.gz
    tdir.mkdir(parents=True, exist_ok=True)
    wd = integ["annotated"]
    script = _script(integ["env"], pipeline, stage, str(tdir / "prov"), data_dir or wd, cmd, launcher, nodes)
    j = J.start(pipeline, stage, stage.replace("_", "-"), script, wd, limit=limit)
    P.set_dashboard(pipeline, str(tdir), live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": str(tdir), "command": cmd, "launcher": launcher, "nodes": nodes}, note=f"running under moirai: {cmd}")
    r = J.wait(pipeline, j["id"], wait)
    return {"status": r["status"], "job": j["id"], "rc": r["rc"], "elapsed": r["elapsed"], "traces": str(tdir), "log_tail": r.get("log_tail", "")[-2500:],
            "note": "the Dashboard tab shows the provenance as it arrives"}


def register(mcp):
    @mcp.tool()
    def moirai_smoke_run(pipeline: str, command: str = "", launcher: str = "", wait: int = 600, limit: int = 1800, batch_script: str = "", queue: str = "pdebug",
                         data_dir: str = "", service_venv: str = "", original_script: str = "") -> dict:
        """Run the annotated app on the small test under moirai-service. Use after integrate. It stops the service however the app ends; limit: seconds before it is
        stopped. The Dashboard tab follows it live. batch_script: path of the generated flux-batch run.sh (Elmerfold), generated WITH --submit-script <wrapper path>
        from moirai_batch_plan; run.sh is not modified: the wrapper <work>/traces/<stage>/submit_moirai.sh is written (mode 755) and a detached job runs
        `flux batch -t <limit/60> --queue <queue> <run.sh>`; run moirai_batch_plan first and get the user's yes. queue: the batch queue. data_dir: MOIRAI_DATA_DIR
        (default the annotated tree). service_venv: venv holding dftracer/bin/dftracer_service. original_script: the app's own submit script the wrapper runs
        (default <ELMER_SRC_DIR from run.sh>/monomer-structure-prediction/submit_smsp.sh). Returns {status, job, rc, elapsed, traces, log_tail}."""
        if batch_script:
            return _start_batch(pipeline, "moirai_smoke", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        return _start(pipeline, "moirai_smoke", command, launcher, 1, "", wait, limit)

    @mcp.tool()
    def moirai_baseline_run(pipeline: str, command: str = "", launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = 3600,
                            batch_script: str = "", queue: str = "pdebug", service_venv: str = "", original_script: str = "") -> dict:
        """Start the real baseline run under moirai-service, in the background, on what the user approved at the gate. command: the full run. launcher: e.g. 'flux run -N4 -n32'.
        nodes: one service starts per node. data_dir: where its I/O is traced. limit: seconds before it is stopped. batch_script, queue, service_venv, original_script: as in
        moirai_smoke_run (batch mode ignores command, launcher, nodes: the run.sh decides). The Dashboard tab shows events as they appear. Returns {status, job, traces}:
        watch with moirai_run_status."""
        if batch_script:
            return _start_batch(pipeline, "baseline", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        if not command:
            raise P.PipelineError("give command (with launcher) or batch_script")
        return _start(pipeline, "baseline", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_batch_plan(pipeline: str, stage: str, batch_script: str = "", queue: str = "pdebug", limit: int = 2400, data_dir: str = "", service_venv: str = "",
                          original_script: str = "") -> dict:
        """DRY RUN of the batch mode: nothing is written, submitted or started. Returns the text of the per-chunk submit wrapper and its path (give that path to
        elmerfold_run_script --submit-script), and, once batch_script (the generated run.sh) is given, checks run.sh (one bulksubmit line, its ':::' input list, the
        wrapper path and the annotated tree in it) and prints the exact submit command and the batch wrapper job text. Show it to the user before moirai_smoke_run /
        moirai_baseline_run. stage: moirai_smoke|baseline. limit: seconds (the batch gets ceil(limit/60) minutes). original_script: default derived from run.sh.
        Returns {status, wrapper_path, wrapper_script, batch_wrapper, run_sh_check, submit_command, resolver, warnings} or {status: error, error}."""
        try:
            _, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, service_venv, original_script)
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}
        if not batch_script:
            r["submit_command"] = r["submit_command"].replace(r["run_script_path"], "<run.sh>")
            r["run_script_path"] = ""
            r["note"] = f"run.sh not given: generate it with elmerfold_run_script --submit-script {r['wrapper_path']}, then plan again with batch_script"
        r["warnings"] += ["UNCONFIRMED: whether `moirai-service start` returns by itself (the wrapper backgrounds it and waits for the ring either way)",
                          "UNCONFIRMED: submit_smsp.sh puts ${ELMER_ENV}/lib first in LD_LIBRARY_PATH for the app (libstdc++ shadowing of moirai/dftracer libs)",
                          "UNCONFIRMED: trace file names per host; whether the GPU server runs on the same node as the driver"]
        r.setdefault("note", "dry run: nothing started or written; the real run writes the wrapper (mode 755), then submits the batch wrapper job, which runs the submit command")
        return r

    @mcp.tool()
    def moirai_batch_render(original: str, env: str, stage_dir: str, data_dir: str = "/data", shm_base: str = "/moirai-agents-test", queue: str = "pdebug", limit: int = 2400,
                            run_sh: str = "", run_script: str = "", annotated: str = "", service_venv: str = "") -> dict:
        """Pure text render, no pipeline or files needed (sandbox checks, experiments). original: path of the app's own submit script; env: moirai_env.sh path;
        stage_dir: <work>/traces/<stage>; shm_base: ring name prefix (the job id is appended at run time). Optional run_sh: text of a generated run.sh to check against
        the wrapper (status error when it does not run the wrapper, has no ':::' or does not hold `annotated`). Returns {status, wrapper_path, wrapper_script,
        batch_wrapper, submit_command, run_sh_check, error?}."""
        return render_batch(original, env, stage_dir, data_dir, shm_base, queue, limit, run_sh, run_script, annotated, service_venv)

    @mcp.tool()
    def moirai_service_resolve(binary: str = "", venv: str = "") -> dict:
        """Find the real dftracer_service for moirai-service, whose --help shows a build-time /tmp/... default. binary: path of moirai-service (default: on PATH).
        venv: venv root to also search. Returns {binary, embedded_relative_path, tried, path, exists}; export MOIRAI_DFTRACER_SERVICE=<path> when the default is missing."""
        r = resolve_dftracer_service(binary, venv)
        r["exists"] = bool(r["path"])
        return r

    @mcp.tool()
    def moirai_run_status(pipeline: str, stage: str) -> dict:
        """Follow a run: job state, log tail and what the trace holds so far (readable while the run is going). Use while it runs, to report progress or spot a stall.
        Returns {job, rc, elapsed, counts, log_tail}."""
        o = P.stage(P.load(pipeline), stage)["outputs"]
        j = J.get(pipeline, o["job"], 2000)
        return {"job": j["id"], "status": j["status"], "rc": j["rc"], "elapsed": j["elapsed"], "timed_out": j.get("timed_out", False), "counts": T.read(o["traces"]).counts(), "log_tail": j["log_tail"]}

    @mcp.tool()
    def moirai_run_stop(pipeline: str, stage: str) -> dict:
        """Stop a run (the service is stopped and the trace closed; in batch mode the batch wrapper job cancels the batch job, whose app jobs get TERM and stop their service).
        Use when the user asks, or the run is clearly stuck. Returns {status, id, stopped}."""
        return J.stop(pipeline, P.stage(P.load(pipeline), stage)["outputs"]["job"])

    @mcp.tool()
    def moirai_validate(pipeline: str, stage: str) -> dict:
        """Validate a moirai run: exit 0, a trace from the service and not spill files, activities and entities recorded, no dangling edge, every entity type in
        the spec seen; in batch mode also one trace per host that ran the wrapper (hosts.txt) and no host with only spill traces. stage: moirai_smoke|baseline.
        Use after the job ended. Returns the verdict {status, failed, unknown, passed, total}."""
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
                "raise limit if it timed out; otherwise compare with the smoke stage: did the annotation break it? Batch mode: read the batch --output file, the chunk logs (moirai-wrapper: lines) and service-<host>-<job>.log")
        files = T.expand(o["traces"])
        v.check("trace", bool(files), "no trace file", "read the service log next to the trace prefix")
        v.check("service_trace", not files or not all("-spill" in f for f in files), "only spill files: no service was running", "check the service log; is /dev/shm big enough?")
        hosts = []
        hf = o.get("hosts_file")
        if hf and os.path.isfile(hf):
            hosts = list(dict.fromkeys(h.strip() for h in Path(hf).read_text().split() if h.strip()))
        if hosts:
            per = {h: [f for f in files if h in os.path.basename(f)] for h in hosts}
            if not any(per.values()):
                v.inconclusive("host_traces", f"no trace file name carries a host name of {hosts}: cannot attribute traces to hosts")
            else:
                missing = [h for h, fs in per.items() if not fs]
                spill = [h for h, fs in per.items() if fs and all("-spill" in f for f in fs)]
                v.check("host_traces", not missing, f"no trace from {missing} (of {hosts})", f"service-<host>-<job>.log next to the traces; did the wrapper start the service on {missing}?")
                v.check("host_service_trace", not spill, f"only spill traces from {spill}", "the app there did not reach the ring: check /dev/shm/<MOIRAI_SERVICE_SHM> on that host and the service log")
            P.record(pipeline, stage, outputs={"per_host_traces": {h: len(fs) for h, fs in per.items()}})
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
