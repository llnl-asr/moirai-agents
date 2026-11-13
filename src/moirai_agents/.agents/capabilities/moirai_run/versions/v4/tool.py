"""Run pipeline stages 5 and 6: run the annotated app under moirai-service, first a smoke run, then the baseline, watched live on the Dashboard.
Two shapes: a plain command (one launcher), or a generated `flux batch` run.sh (batch mode: the script is rewritten, never guessed)."""
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


class ShapeError(ValueError):
    """run.sh does not have the shape the rewrite relies on."""


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
for _ in $(seq 100); do [ -s \"/dev/shm$MOIRAI_SERVICE_SHM\" ] && break; sleep 0.1; done
[ -s \"/dev/shm$MOIRAI_SERVICE_SHM\" ] || echo \"moirai-service ring not visible here: events may go to spill files\" >&2
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


# ---------------------------------------------------------------- batch rewrite (pure)
_PRE = r'''
# --- __MARK__ ---
source __ENV__
export MOIRAI_LOG_FILE=__LOG__
export MOIRAI_DATA_DIR=__DATA__
export MOIRAI_SERVICE_SHM=__SHM__
export MOIRAI_PC_BLOCKS=${MOIRAI_PC_BLOCKS:-16}
export MOIRAI_PC_FLUSH_MS=${MOIRAI_PC_FLUSH_MS:-250}
# moirai-service --help prints a build-time default for dftracer_service; when it does not exist, point to the installed one
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  _moirai_dflt="$(moirai-service --help 2>&1 | grep -o '/[^[:space:]]*dftracer_service' | head -1)"
  if [ -z "${_moirai_dflt}" ] || [ ! -x "${_moirai_dflt}" ]; then
    _moirai_real="$(readlink -f "$(command -v moirai-service)")"
    for _c in "$(dirname "${_moirai_real}")/../../dftracer/bin/dftracer_service" __VENVGLOB__; do
      if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
    done
    if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then echo "moirai: no dftracer_service found (default ${_moirai_dflt:-unknown} missing)" >&2; exit 1; fi
  fi
fi
_moirai_hosts=()
_moirai_started=0
moirai_stop() {
  trap - EXIT TERM INT
  if [ "${_moirai_started}" = 1 ]; then
    timeout 120 flux exec -r all moirai-service stop || echo "moirai: moirai-service stop failed on at least one rank" >&2
  fi
}
trap moirai_stop EXIT
trap 'exit 143' TERM INT
# --- end moirai preamble ---
'''

_SVC = r'''
# --- moirai-service on every rank (generated, after the drain section) ---
# flux exec bypasses the scheduler: no job is created, no core is held, and the drained launch node is reached too.
_moirai_hosts=($(flux exec -r all hostname | sort))
if [ "${#_moirai_hosts[@]}" -eq 0 ]; then echo "moirai: flux exec -r all hostname returned no host" >&2; exit 1; fi
printf '%s\n' "${_moirai_hosts[@]}" > __HOSTS__
export MOIRAI_HOSTS="${_moirai_hosts[*]}"
echo "MOIRAI_HOSTS=${MOIRAI_HOSTS}"
_moirai_started=1
flux exec -r all bash -c 'setsid nohup moirai-service start >> "$1/service-$(hostname).log" 2>&1 < /dev/null & echo started' _ __TRACES__ \
  || { echo "moirai: flux exec could not start moirai-service on every rank" >&2; exit 1; }
flux exec -r all bash -c 'for _i in $(seq 1 "$1"); do [ -s "/dev/shm$MOIRAI_SERVICE_SHM" ] && exit 0; sleep 1; done; echo "moirai: ring /dev/shm$MOIRAI_SERVICE_SHM not visible on $(hostname) after $1 s (service log: $2/service-$(hostname).log)" >&2; exit 1' _ __WAIT__ __TRACES__ \
  || { echo "moirai: ring missing on at least one rank (host named above)" >&2; exit 1; }
export MOIRAI_SERVICES_READY=1
echo "MOIRAI_SERVICES_READY=1 hosts: ${MOIRAI_HOSTS}"
# --- end moirai-service on every rank ---
'''


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
  echo "wrapper: batch job $jid FAILED (result=$res returncode=$rc); see its --output file and the service logs next to the traces" >&2
  case "$rc" in ''|*[!0-9]*) exit 1 ;; 0) exit 1 ;; *) exit "$rc" ;; esac
fi
exit 0
"""


def _shape(lines):
    """Insertion indices by pattern; ShapeError when run.sh is not the generated shape."""
    if any(MARKER in l for l in lines):
        raise ShapeError("run.sh already holds a moirai preamble: pass the original generated run.sh")
    hdr = [i for i, l in enumerate(lines) if l.startswith("#flux:")]
    if not hdr:
        raise ShapeError("no '#flux:' header lines: not a generated flux batch run.sh (utils/gen_launch_script.py)")
    cmd = next((i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith("#")), len(lines))
    if hdr[-1] > cmd:
        raise ShapeError(f"'#flux:' header line {hdr[-1] + 1} comes after the first command (line {cmd + 1}): flux ignores it, the header is not contiguous")
    pats = {"crit_ranks": r"^\s*crit_ranks=\$\(flux getattr broker\.critical-ranks\)", "crit_nodes": r"^\s*crit_nodes=",
            "num_nodes_adjust": r"^\s*num_nodes=\$\(\(\s*num_nodes\s*-\s*num_crit_nodes\s*\)\)", "drain": r"^\s*flux resource drain\b"}
    at = {}
    for k, p in pats.items():
        m = [i for i, l in enumerate(lines) if re.search(p, l)]
        if len(m) != 1:
            raise ShapeError(f"drain section: expected exactly one line matching /{p}/ for {k}, found {len(m)}")
        at[k] = m[0]
    if not at["crit_ranks"] < at["crit_nodes"] < at["num_nodes_adjust"] < at["drain"]:
        raise ShapeError(f"drain section lines are out of order: {at}")
    return hdr[-1], at["drain"]


def render_batch(run_sh: str, env: str, traces: str, data_dir: str, shm: str, queue: str, limit: int, stage: str = "moirai_smoke",
                 modules=("mpifileutils/0.11.1",), venv: str = "", wait_s: int = 60, run_script: str = "") -> dict:
    """Pure: the original run.sh text -> the rewritten run.sh, the wrapper, the submit command, warnings. Never touches the cluster."""
    q = shlex.quote
    lines = run_sh.split("\n")
    h_last, drain = _shape(lines)
    tdir = str(Path(traces).parent)               # traces = <stage>/raw
    rs = run_script or str(Path(tdir) / "run-moirai.sh")
    venvglob = f"{q(venv)}/lib/python*/site-packages/dftracer/bin/dftracer_service" if venv else ""
    pre = (_PRE.replace("__MARK__", MARKER).replace("__ENV__", q(env)).replace("__LOG__", q(str(Path(traces) / "prov")))
           .replace("__DATA__", q(data_dir)).replace("__SHM__", shm).replace("__VENVGLOB__", venvglob))
    svc = (_SVC.replace("__HOSTS__", q(str(Path(tdir) / "hosts.txt"))).replace("__TRACES__", q(traces)).replace("__WAIT__", str(int(wait_s))))
    out = lines[:h_last + 1] + pre.split("\n") + lines[h_last + 1:drain + 1] + svc.split("\n") + lines[drain + 1:]
    minutes = max(1, math.ceil(int(limit) / 60))
    submit = f"flux batch -t {minutes} --queue {q(queue)} {q(rs)}"
    warnings = []
    if not venv:
        warnings.append("no service venv given: dftracer_service is looked up only relative to the moirai-service binary")
    script = "\n".join(out)
    return {"status": "ok", "run_script": script, "run_script_path": rs, "wrapper": _wrapper(rs, minutes, queue, list(modules), stage),
            "submit_command": submit, "minutes": minutes, "preamble_after_line": h_last + 1, "services_after_original_line": drain + 1,
            "warnings": warnings, "hosts_file": str(Path(tdir) / "hosts.txt"), "service_mechanism": "flux exec -r all",
            "scheduled_service_jobs": "flux submit" in script}


def _plan(pipeline, stage, batch_script, queue, limit, data_dir, modules, venv):
    c = P.load(pipeline)
    P.require_ready(c, stage)
    integ = P.stage(c, "integrate")["outputs"]
    if not integ.get("env"):
        raise P.PipelineError("the integrate stage has not written moirai_env.sh")
    src = Path(batch_script).expanduser()
    if not src.is_file():
        raise P.PipelineError(f"batch_script {batch_script} does not exist")
    sdir = P.work(c) / "traces" / stage
    shm = f"/moirai-agents-{pipeline[-20:]}-{stage}".replace(".", "-")
    r = render_batch(src.read_text(), integ["env"], str(sdir / "raw"), data_dir or integ["annotated"], shm, queue, limit, stage, modules, venv)
    r["traces"], r["stage_dir"], r["annotated"] = str(sdir / "raw"), str(sdir), integ["annotated"]
    return c, r


def _start_batch(pipeline, stage, batch_script, queue, limit, data_dir, modules, venv, wait):
    try:
        c, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, modules, venv)
    except ShapeError as e:
        raise P.PipelineError(f"run.sh changed shape, nothing was started: {e}")
    Path(r["traces"]).mkdir(parents=True, exist_ok=True)
    Path(r["run_script_path"]).write_text(r["run_script"])
    j = J.start(pipeline, stage, stage.replace("_", "-") + "-batch", r["wrapper"], r["annotated"], limit=int(limit) + 300)
    P.set_dashboard(pipeline, r["traces"], live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": r["traces"], "batch_script": str(batch_script), "run_script": r["run_script_path"],
                                                        "hosts_file": r["hosts_file"], "submit_command": r["submit_command"], "queue": queue, "mode": "batch"},
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
                         data_dir: str = "", service_venv: str = "") -> dict:
        """Run the annotated app on the small test under moirai-service. Use after integrate. It stops the service however the app ends; limit: seconds before it is
        stopped. The Dashboard tab follows it live. batch_script: path of a generated flux-batch run.sh (Elmerfold): then the script is rewritten with moirai and
        submitted as `flux batch -t <limit/60> --queue <queue>` from a wrapper job; run moirai_batch_plan first and get the user's yes. queue: the batch queue.
        data_dir: MOIRAI_DATA_DIR (default the annotated tree). service_venv: venv holding dftracer/bin/dftracer_service. Returns {status, job, rc, elapsed, traces, log_tail}."""
        if batch_script:
            return _start_batch(pipeline, "moirai_smoke", batch_script, queue, limit, data_dir, ("mpifileutils/0.11.1",), service_venv, wait)
        return _start(pipeline, "moirai_smoke", command, launcher, 1, "", wait, limit)

    @mcp.tool()
    def moirai_baseline_run(pipeline: str, command: str = "", launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = 3600,
                            batch_script: str = "", queue: str = "pdebug", service_venv: str = "") -> dict:
        """Start the real baseline run under moirai-service, in the background, on what the user approved at the gate. command: the full run. launcher: e.g. 'flux run -N4 -n32'.
        nodes: one service starts per node. data_dir: where its I/O is traced. limit: seconds before it is stopped. batch_script, queue, service_venv: as in
        moirai_smoke_run (batch mode ignores command, launcher, nodes: the run.sh decides). The Dashboard tab shows events as they appear. Returns {status, job, traces}:
        watch with moirai_run_status."""
        if batch_script:
            return _start_batch(pipeline, "baseline", batch_script, queue, limit, data_dir, ("mpifileutils/0.11.1",), service_venv, wait)
        if not command:
            raise P.PipelineError("give command (with launcher) or batch_script")
        return _start(pipeline, "baseline", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_batch_plan(pipeline: str, stage: str, batch_script: str, queue: str = "pdebug", limit: int = 2400, data_dir: str = "", service_venv: str = "") -> dict:
        """DRY RUN of the batch mode: nothing is written, submitted or started. Reads the generated run.sh and returns the rewritten script, the wrapper job script, the
        exact submit command and warnings. Show it to the user before moirai_smoke_run / moirai_baseline_run with batch_script.
        stage: moirai_smoke|baseline. limit: seconds (the batch gets ceil(limit/60) minutes). Returns {status, run_script, wrapper, submit_command, warnings,
        service_mechanism, scheduled_service_jobs, ...} or {status: error, error} when run.sh changed shape."""
        try:
            _, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, ("mpifileutils/0.11.1",), service_venv)
        except ShapeError as e:
            return {"status": "error", "error": f"run.sh changed shape, nothing generated: {e}"}
        r["note"] = "dry run: nothing started; the real run writes run_script to run_script_path and submits the wrapper as a detached job"
        return r

    @mcp.tool()
    def moirai_batch_render(run_sh: str, env: str, traces: str, data_dir: str = "/data", shm: str = "/moirai-agents-test", queue: str = "pdebug", limit: int = 2400,
                            service_venv: str = "") -> dict:
        """Pure rewrite of run.sh text, no pipeline needed (used by the sandbox checks and for experiments). run_sh: the text; env: moirai_env.sh path; traces: the
        <stage>/raw folder. Returns the same as moirai_batch_plan, or {status: error, error} when run.sh changed shape."""
        try:
            return render_batch(run_sh, env, traces, data_dir, shm, queue, limit, "moirai_smoke", ("mpifileutils/0.11.1",), service_venv)
        except ShapeError as e:
            return {"status": "error", "error": f"run.sh changed shape, nothing generated: {e}"}

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
        """Stop a run (the service is stopped and the trace closed; in batch mode the wrapper cancels the batch job). Use when the user asks, or the run is clearly stuck.
        Returns {status, id, stopped}."""
        return J.stop(pipeline, P.stage(P.load(pipeline), stage)["outputs"]["job"])

    @mcp.tool()
    def moirai_validate(pipeline: str, stage: str) -> dict:
        """Validate a moirai run: exit 0, a trace from the service and not spill files, activities and entities recorded, no dangling edge, every entity type in
        the spec seen; in batch mode also one trace per host and no host with only spill traces. stage: moirai_smoke|baseline. Use after the job ended.
        Returns the verdict {status, failed, unknown, passed, total}."""
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
                "raise limit if it timed out; otherwise compare with the smoke stage: did the annotation break it? Batch mode: read the batch --output file and service-<host>.log")
        files = T.expand(o["traces"])
        v.check("trace", bool(files), "no trace file", "read the service log next to the trace prefix")
        v.check("service_trace", not files or not all("-spill" in f for f in files), "only spill files: no service was running", "check the service log; is /dev/shm big enough?")
        hosts = []
        hf = o.get("hosts_file")
        if hf and os.path.isfile(hf):
            hosts = [h.strip() for h in Path(hf).read_text().split() if h.strip()]
        if hosts:
            per = {h: [f for f in files if h in os.path.basename(f)] for h in hosts}
            if not any(per.values()):
                v.inconclusive("host_traces", f"no trace file name carries a host name of {hosts}: cannot attribute traces to hosts")
            else:
                missing = [h for h, fs in per.items() if not fs]
                spill = [h for h, fs in per.items() if fs and all("-spill" in f for f in fs)]
                v.check("host_traces", not missing, f"no trace from {missing} (of {hosts})", f"service-<host>.log next to the traces; was the service started and the app run on {missing}? (placement is unconfirmed)")
                v.check("host_service_trace", not spill, f"only spill traces from {spill}", "the app there did not reach the ring: check /dev/shm/<MOIRAI_SERVICE_SHM> on that host and that the app env carries MOIRAI_SERVICE_SHM")
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
