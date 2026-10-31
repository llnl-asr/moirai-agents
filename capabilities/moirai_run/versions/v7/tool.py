"""Run pipeline stages 5 and 6: run the annotated app under moirai-service, first a smoke run, then the baseline, watched live on the Dashboard.
Two shapes: a plain command (one launcher), or a generated `flux batch` run.sh (batch mode): run.sh stays UNMODIFIED; its app job runs a generated
per-chunk submit wrapper (--submit-script) that starts moirai-service inside the job's own shell. The wrapper is written beforehand by moirai_wrapper_write."""
import glob
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGES = ("moirai_smoke", "baseline")
MARKER = "moirai preamble (generated)"
DFT_REL = "../../dftracer/bin/dftracer_service"
WRAPPER_NAME = "submit_moirai.sh"
ORIGINAL_REL = "monomer-structure-prediction/submit_smsp.sh"   # Elmerfold's per-chunk submit script, under ELMER_SRC_DIR
RC_CHUNK_FAILED, RC_HOSTS_MISSING, RC_HOSTS_EMPTY, RC_OUTPUT_MISSING = 20, 21, 22, 23


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
def venv_root_from_python(python: str) -> str:
    """Pure: <venv> from <venv>/bin/python (the path is NOT resolved, so a venv symlinking to a system python still gives the venv). '' when it is not under a bin dir."""
    if not python:
        return ""
    p = Path(os.path.expanduser(str(python).strip()))
    return str(p.parent.parent) if p.parent.name == "bin" else ""


def venv_from_integrate(integ: dict) -> str:
    """Pure: venv root of the moirai python recorded by the integrate stage (outputs moirai.python, as {moirai: {python}} or a flat key)."""
    m = integ.get("moirai")
    py = (m.get("python") if isinstance(m, dict) else "") or integ.get("moirai.python") or integ.get("moirai_python") or ""
    return venv_root_from_python(py)


def resolve_dftracer_service(binary: str = "", venv: str = "") -> dict:
    """Where the real dftracer_service is. moirai-service in a venv is a Python entry-point script (`#!<venv>/bin/python`), NOT the compiled binary, so a path
    relative to it is wrong. Candidates in order: <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service (venv given, else the venv named by the script's shebang),
    then, only for a compiled binary that embeds ../../dftracer/bin/dftracer_service, that path relative to its real path."""
    b = binary or shutil.which("moirai-service") or ""
    tried, found, embedded, shebang_venv = [], "", False, ""
    head = b""
    if b and os.path.isfile(b):
        real = os.path.realpath(b)
        try:
            head = Path(b).read_bytes()[:300]
        except OSError:
            head = b""
        if head.startswith(b"#!"):
            first = head.split(b"\n", 1)[0][2:].decode(errors="replace").split()
            shebang_venv = venv_root_from_python(first[0]) if first else ""
        else:
            try:
                embedded = DFT_REL.encode() in Path(real).read_bytes()
            except OSError:
                embedded = False
    for v in (venv, shebang_venv):
        if v:
            tried += sorted(glob.glob(os.path.join(os.path.expanduser(v), "lib", "python*", "site-packages", "dftracer", "bin", "dftracer_service")))
    if embedded:
        tried.append(os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(b)), DFT_REL)))
    for c in dict.fromkeys(tried):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            found = os.path.realpath(c)
            break
    return {"binary": b, "is_script": head.startswith(b"#!"), "embedded_relative_path": embedded, "shebang_venv": shebang_venv, "tried": list(dict.fromkeys(tried)), "path": found}


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
# 1. an explicit MOIRAI_DFTRACER_SERVICE wins. 2. moirai-service --help prints a build-time default: use it when it exists.
# 3. else the dftracer package of the python on PATH (the venv's, after sourcing the env file): <pkg>/bin/dftracer_service. moirai-service in a venv is a
#    Python entry-point script, so a path relative to it is wrong.
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  _dflt="$(moirai-service --help 2>&1 | grep -o '/[^[:space:]]*dftracer_service' | head -1)"
  if [ -z "${_dflt}" ] || [ ! -x "${_dflt}" ]; then
    _py="$(command -v python 2>/dev/null || command -v python3 2>/dev/null)"
    _pkg=""
    [ -n "${_py}" ] && _pkg="$("${_py}" -c 'import dftracer,os;print(os.path.dirname(dftracer.__file__))' 2>/dev/null)"
    for _c in __RESOLVED__"${_pkg:+${_pkg}/bin/dftracer_service}"__VENVGLOB__; do
      if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
    done
    [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service found (default ${_dflt:-unknown} missing; python ${_py:-none}, dftracer package ${_pkg:-not importable})" >&2; exit 2; }
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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def wrapper_state(stage_dir, text: str) -> dict:
    """Read-only: is <stage_dir>/submit_moirai.sh missing, equal to `text`, or different? Returns {state: missing|in_sync|differs, path, sha256, on_disk_sha256}."""
    p = Path(str(stage_dir)) / WRAPPER_NAME
    cur = None
    if p.is_file():
        try:
            cur = p.read_text()
        except OSError:
            cur = None
    st = "missing" if cur is None else ("in_sync" if cur == text else "differs")
    return {"state": st, "path": str(p), "sha256": _sha(text), "on_disk_sha256": _sha(cur) if cur is not None else ""}


def sync_wrapper(stage_dir, text: str) -> dict:
    """Write `text` to <stage_dir>/submit_moirai.sh (mode 755) and create <stage_dir>/raw. Idempotent: equal text and mode 755 change nothing.
    Returns {action: created|unchanged|rewritten|mode_fixed, path, sha256, previous_sha256, raw_dir}; `rewritten` means a different file was on disk."""
    sd = Path(str(stage_dir))
    raw = sd / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    p = sd / WRAPPER_NAME
    prev = ""
    if p.is_file():
        cur = p.read_text()
        prev = _sha(cur)
        if cur == text:
            if (p.stat().st_mode & 0o777) != 0o755:
                p.chmod(0o755)
                action = "mode_fixed"
            else:
                action = "unchanged"
            return {"action": action, "path": str(p), "sha256": _sha(text), "previous_sha256": prev, "raw_dir": str(raw)}
        action = "rewritten"
    else:
        action = "created"
    p.write_text(text)
    p.chmod(0o755)
    return {"action": action, "path": str(p), "sha256": _sha(text), "previous_sha256": prev, "raw_dir": str(raw)}


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


def batch_output_path(run_sh: str) -> str:
    """Pure: the path of the `#flux: --output <path>` line of a generated run.sh ('' when there is none)."""
    m = re.search(r"^\s*#flux:\s+--output(?:=|\s+)(\S+)", run_sh or "", re.M)
    return m.group(1).strip("'\"") if m else ""


# The batch job's return code only says run.sh ended normally: a failed `flux bulksubmit` chunk job does NOT make it non-zero. So after the batch the wrapper job
# reads the batch --output file and hosts.txt. Distinct codes: 20 chunk job failed, 21 hosts.txt missing, 22 hosts.txt empty, 23 --output file missing.
_VERIFY = r'''_out=__OUT__
_hosts=__HOSTS__
if [ -z "${_out}" ]; then
  echo "wrapper: warning: run.sh has no '#flux: --output' line: failed chunk jobs cannot be detected from the batch log" >&2
elif [ ! -f "${_out}" ]; then
  echo "wrapper: batch job $jid COMPLETED but its --output file ${_out} does not exist: cannot tell whether the chunk jobs ran" >&2
  exit 23
else
  _bad="$(grep -E 'exited with exit code [1-9][0-9]*| failed[[:space:]]*$' "${_out}" | head -5)"
  if [ -n "${_bad}" ]; then
    _cid="$(grep -E ' failed[[:space:]]*$' "${_out}" | head -1 | sed -E 's/^.*: ([^ ]+) failed[[:space:]]*$/\1/')"
    echo "wrapper: batch job $jid COMPLETED rc 0 but a bulksubmit chunk job FAILED (chunk job id ${_cid:-unknown}); the batch return code does not reflect it. Log ${_out}; chunk log and service logs next to the traces (moirai-wrapper: lines):" >&2
    echo "${_bad}" >&2
    exit 20
  fi
fi
if [ ! -e "${_hosts}" ]; then
  echo "wrapper: batch job $jid COMPLETED but ${_hosts} does not exist: no per-chunk wrapper (submit_moirai.sh) ran; was run.sh generated with --submit-script?" >&2
  exit 21
fi
if [ ! -s "${_hosts}" ]; then
  echo "wrapper: batch job $jid COMPLETED but ${_hosts} is empty: no per-chunk wrapper ran" >&2
  exit 22
fi
'''


def batch_verify_run(run_out: str, hosts: str = "ok", out_present: bool = True) -> dict:
    """Run the real post-batch verification snippet with bash on temp files (nothing else is touched). run_out: text of the batch --output file. hosts: ok|empty|missing.
    out_present False: the --output file does not exist. Returns {rc, stderr, chunk_job_id}."""
    with tempfile.TemporaryDirectory(prefix="moirai_batch_verify_") as td:
        t = Path(td)
        out, hf = t / "run.out", t / "hosts.txt"
        if out_present:
            out.write_text(run_out)
        if hosts == "ok":
            hf.write_text("tuolumne1\n")
        elif hosts == "empty":
            hf.write_text("")
        sh = t / "verify.sh"
        sh.write_text("jid=TESTJOB\n" + _VERIFY.replace("__OUT__", shlex.quote(str(out))).replace("__HOSTS__", shlex.quote(str(hf))) + "exit 0\n")
        r = subprocess.run(["bash", str(sh)], capture_output=True, text=True, timeout=30)
    m = re.search(r"chunk job id (\S+?)\)", r.stderr)
    return {"rc": r.returncode, "stderr": r.stderr, "chunk_job_id": m.group(1) if m else ""}


def _wrapper(run_script, minutes, queue, modules, name, out_path="", hosts_path=""):
    q = shlex.quote
    mods = " ".join(q(m) for m in modules)
    head = f"""set -uo pipefail
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
"""
    return head + _VERIFY.replace("__OUT__", q(out_path)).replace("__HOSTS__", q(hosts_path)) + "echo \"wrapper: batch job $jid verified: no failed chunk job, hosts recorded\"\nexit 0\n"


def render_batch(original, env, stage_dir, data_dir="", shm_base="/moirai-agents-run", queue="pdebug", limit=2400, run_sh="", run_script="",
                 annotated="", venv="", dft_service="", modules=("mpifileutils/0.11.1",), wait_s=60, stage="moirai_smoke") -> dict:
    """Pure: wrapper text, batch wrapper text, exact submit command, and (when run_sh text is given) the run.sh check. Never touches the cluster."""
    sd = str(stage_dir).rstrip("/")
    wpath = f"{sd}/{WRAPPER_NAME}"
    minutes = max(1, math.ceil(int(limit) / 60))
    rs = run_script or f"{str(Path(sd).parent)}/run.sh"
    submit = f"flux batch -t {minutes} --queue {shlex.quote(queue)} {shlex.quote(rs)}"
    outp = batch_output_path(run_sh)
    r = {"status": "ok", "wrapper_path": wpath, "wrapper_script": render_submit_wrapper(original, env, sd, data_dir, shm_base, wait_s, venv, dft_service),
         "batch_wrapper": _wrapper(rs, minutes, queue, list(modules), stage, outp, f"{sd}/hosts.txt"), "submit_command": submit, "minutes": minutes, "run_script_path": rs,
         "original_script": original, "hosts_file": f"{sd}/hosts.txt", "batch_output": outp, "traces": f"{sd}/raw", "warnings": []}
    if not venv and not dft_service:
        r["warnings"].append("no service venv given and dftracer_service not resolved here: the wrapper can only use the dftracer package of the python on PATH")
    if run_sh and not outp:
        r["warnings"].append("run.sh has no '#flux: --output' line: the batch wrapper job cannot detect failed chunk jobs from the batch log")
    if run_sh:
        chk = check_run_sh(run_sh, wpath, annotated)
        r["run_sh_check"] = chk
        if not chk["ok"]:
            r["status"] = "error"
            r["error"] = "run.sh check failed: " + "; ".join(chk["problems"])
    return r


def _plan(pipeline, stage, batch_script, queue, limit, data_dir, venv, original="", strict=False):
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
        # the annotated copy's own submit script first (same answer with or without run.sh, so the wrapper text is stable), then run.sh's ELMER_SRC_DIR
        cand = os.path.join(str(integ.get("annotated", "")), ORIGINAL_REL) if integ.get("annotated") else ""
        m = re.search(r"^\s*(?:export\s+)?ELMER_SRC_DIR=(\S+)", run_sh, re.M)
        if cand and Path(cand).is_file():
            original = cand
        elif m:
            original = os.path.join(m.group(1).strip("'\""), ORIGINAL_REL)
        else:
            raise P.PipelineError(f"cannot tell the original submit script: {cand or ORIGINAL_REL} does not exist; give original_script")
    if not Path(original).is_file():
        raise P.PipelineError(f"original submit script {original} does not exist")
    venv_src = "argument" if venv else ""
    if not venv:
        venv = venv_from_integrate(integ)
        venv_src = "integrate outputs moirai.python" if venv else ""
    res = resolve_dftracer_service("", venv)
    if strict and not res["path"]:
        raise P.PipelineError(
            f"dftracer_service not found: looked in {res['tried'] or [(venv or '<no venv>') + '/lib/python*/site-packages/dftracer/bin/dftracer_service']} "
            f"(venv {venv or 'unknown'}, from {venv_src or 'nothing: integrate outputs have no moirai.python'}). Fix: pass service_venv=<the venv root of the moirai python> "
            "that has dftracer installed (pip install dftracer there), or re-run integrate_apply; the in-wrapper fallback is not relied on for a first run")
    r = render_batch(original, integ["env"], str(sdir), data_dir or integ["annotated"], shm, queue, limit, run_sh, rs, integ["annotated"], venv, res["path"], stage=stage)
    r["resolver"] = res
    r["service_venv"], r["service_venv_source"] = venv, venv_src
    r["stage_dir"], r["annotated"] = str(sdir), integ["annotated"]
    return c, r


def _start_batch(pipeline, stage, batch_script, queue, limit, data_dir, venv, wait, original):
    if not batch_script:
        raise P.PipelineError("give batch_script (the generated run.sh)")
    c, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, venv, original, strict=True)
    if r["status"] != "ok":
        raise P.PipelineError(f"nothing was started: {r['error']}")
    wr = sync_wrapper(r["stage_dir"], r["wrapper_script"])     # verifies the file on disk equals the fresh render; rewrites it when it differs
    note_w = ""
    if wr["action"] == "rewritten":
        note_w = (f"; the wrapper on disk differed from the fresh render and was REWRITTEN ({wr['previous_sha256'][:12]} -> {wr['sha256'][:12]}): "
                  "pass the same service_venv/data_dir/original_script as in moirai_wrapper_write to avoid it")
    elif wr["action"] == "created":
        note_w = "; the wrapper did not exist and was created (run moirai_wrapper_write before generating run.sh)"
    Path(r["hosts_file"]).unlink(missing_ok=True)
    j = J.start(pipeline, stage, stage.replace("_", "-") + "-batch", r["batch_wrapper"], r["annotated"], limit=int(limit) + 300)
    P.set_dashboard(pipeline, r["traces"], live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": r["traces"], "batch_script": str(batch_script), "run_script": r["run_script_path"],
                                                        "wrapper": r["wrapper_path"], "wrapper_sha256": wr["sha256"], "hosts_file": r["hosts_file"],
                                                        "batch_output": r["batch_output"], "service_venv": r["service_venv"],
                                                        "submit_command": r["submit_command"], "queue": queue, "mode": "batch"},
             note=f"submitted as a flux batch: {r['submit_command']}{note_w}")
    w = J.wait(pipeline, j["id"], wait)
    return {"status": w["status"], "job": j["id"], "rc": w["rc"], "elapsed": w["elapsed"], "traces": r["traces"], "submit_command": r["submit_command"],
            "wrapper": wr, "log_tail": w.get("log_tail", "")[-2500:], "note": "the Dashboard tab shows the provenance as it arrives" + note_w}


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


# Real content of /p/lustre5/haridev/elmerfold/runs/moirai-smoke/run.out, batch f3hFu78obrzb, 2026-10-30 (the failed run)
REAL_FAILED_RUN_OUT = ("flux-job: task(s) exited with exit code 2\n"
                       "Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\n"
                       "flux-job: there are no more waitable jobs\n"
                       "All jobs completed (or failed)!\n")
# A clean log has no non-zero exit code line and no line ending in ' failed' ('(or failed)!' does not end with it). Synthetic: no clean real run exists yet.
CLEAN_RUN_OUT = ("Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 done\n"
                 "flux-job: there are no more waitable jobs\n"
                 "All jobs completed (or failed)!\n")


def _selftest() -> dict:
    """Wrapper write, idempotence, mismatch handling, resolver and post-batch verification on temp dirs. Nothing else is touched."""
    cases = {}
    with tempfile.TemporaryDirectory(prefix="moirai_wrapper_selftest_") as td:
        sd = Path(td) / "traces" / "moirai_smoke"
        txt = render_submit_wrapper("/ann/monomer-structure-prediction/submit_smsp.sh", "/p/moirai_env.sh", str(sd), "/data", "/moirai-agents-t-moirai_smoke")
        cases["missing_before"] = wrapper_state(sd, txt)["state"] == "missing"
        a = sync_wrapper(sd, txt)
        p = sd / WRAPPER_NAME
        cases["created"] = a["action"] == "created" and p.read_text() == txt
        cases["sha"] = a["sha256"] == hashlib.sha256(txt.encode()).hexdigest()
        cases["dirs"] = (sd / "raw").is_dir()
        cases["mode_755"] = (p.stat().st_mode & 0o777) == 0o755
        m1 = p.stat().st_mtime_ns
        b = sync_wrapper(sd, txt)
        cases["idempotent"] = b["action"] == "unchanged" and b["sha256"] == a["sha256"] and p.stat().st_mtime_ns == m1
        cases["in_sync"] = wrapper_state(sd, txt)["state"] == "in_sync"
        p.write_text(txt + "# edited\n")
        cases["differs_seen"] = wrapper_state(sd, txt)["state"] == "differs"
        c = sync_wrapper(sd, txt)
        cases["mismatch_rewritten"] = c["action"] == "rewritten" and p.read_text() == txt and c["previous_sha256"] != c["sha256"]
        txt2 = render_submit_wrapper("/ann/monomer-structure-prediction/submit_smsp.sh", "/p/moirai_env.sh", str(sd), "/data", "/moirai-agents-t-moirai_smoke", venv="/v")
        cases["venv_changes_text"] = txt2 != txt and sync_wrapper(sd, txt2)["action"] == "rewritten"
        p.chmod(0o644)
        cases["mode_fixed"] = sync_wrapper(sd, txt2)["action"] == "mode_fixed" and (p.stat().st_mode & 0o777) == 0o755
        wp = str(p)
        ok = check_run_sh(f"#!/bin/bash\nexport ELMER_SRC_DIR=/ann\nflux bulksubmit --nodes 1 {wp} '{{}}' ::: /r/c.csv\n", wp, "/ann")
        bad = check_run_sh("#!/bin/bash\nexport ELMER_SRC_DIR=/ann\nflux bulksubmit --nodes 1 /ann/monomer-structure-prediction/submit_smsp.sh '{}' ::: /r/c.csv\n", wp, "/ann")
        cases["run_sh_ok"] = ok["ok"]
        cases["run_sh_without_wrapper_refused"] = (not bad["ok"]) and "does not reference the wrapper" in " ".join(bad["problems"])
        # D2: the in-wrapper fallback goes through python, not through the moirai-service script path
        cases["wrapper_python_resolver"] = ("import dftracer,os" in txt and "dirname \"${_real}\"" not in txt and "readlink -f \"$(command -v moirai-service)\"" not in txt)
        # D2: a venv whose moirai-service is a Python entry-point script resolves to site-packages/dftracer/bin/dftracer_service
        v = Path(td) / "venv"
        (v / "bin").mkdir(parents=True)
        svc = v / "lib" / "python3.11" / "site-packages" / "dftracer" / "bin"
        svc.mkdir(parents=True)
        (svc / "dftracer_service").write_text("#!/bin/sh\n")
        (svc / "dftracer_service").chmod(0o755)
        script = v / "bin" / "moirai-service"
        script.write_text(f"#!{v}/bin/python\nfrom moirai.service import main\n")
        script.chmod(0o755)
        rs = resolve_dftracer_service(str(script), "")
        cases["venv_from_shebang"] = rs["path"] == os.path.realpath(svc / "dftracer_service") and rs["is_script"] and rs["shebang_venv"] == str(v)
        cases["venv_from_integrate"] = venv_from_integrate({"moirai": {"python": "/x/venv/bin/python"}}) == "/x/venv" and venv_from_integrate({"moirai.python": "/y/bin/python"}) == "/y" and venv_from_integrate({}) == ""
        (svc / "dftracer_service").chmod(0o644)
        cases["not_executable_not_found"] = resolve_dftracer_service("", str(v))["path"] == ""
    # D1: the real post-batch verification snippet, run with bash
    real = batch_verify_run(REAL_FAILED_RUN_OUT)
    cases["batch_output_fail"] = real["rc"] == RC_CHUNK_FAILED and real["chunk_job_id"] == "f35QqToh9" and "run.out" in real["stderr"]
    cases["batch_output_clean_pass"] = batch_verify_run(CLEAN_RUN_OUT)["rc"] == 0
    cases["hosts_missing_code"] = batch_verify_run(CLEAN_RUN_OUT, "missing")["rc"] == RC_HOSTS_MISSING
    cases["hosts_empty_code"] = batch_verify_run(CLEAN_RUN_OUT, "empty")["rc"] == RC_HOSTS_EMPTY
    cases["output_missing_code"] = batch_verify_run("", "ok", False)["rc"] == RC_OUTPUT_MISSING
    cases["exit_code_0_not_failure"] = batch_verify_run("flux-job: task(s) exited with exit code 0\nAll jobs completed (or failed)!\n")["rc"] == 0
    cases["output_path_parsed"] = batch_output_path("#!/bin/bash\n#flux: --output /p/runs/moirai-smoke/run.out\n") == "/p/runs/moirai-smoke/run.out" and batch_output_path("#!/bin/bash\n") == ""
    failed = [k for k, vv in cases.items() if not vv]
    line = "moirai wrapper selftest PASS " + " ".join(f"{k}=ok" for k in cases) if not failed else "moirai wrapper selftest FAIL " + ",".join(failed)
    return {"status": "ok" if not failed else "error", "line": line, "cases": cases}


def register(mcp):
    @mcp.tool()
    def moirai_wrapper_write(pipeline: str, stage: str, service_venv: str = "", data_dir: str = "", original_script: str = "") -> dict:
        """Write ONLY the per-chunk submit wrapper <work>/traces/<stage>/submit_moirai.sh (mode 755) and create the stage and raw dirs. Use right after
        integrate, BEFORE elmerfold_run_script: the run.sh generator requires --submit-script to be an existing file. The text is the one moirai_batch_plan shows.
        It submits and starts nothing. Idempotent: the same text changes nothing (action unchanged); a different file on disk is rewritten (action rewritten).
        pipeline: pipeline id. stage: moirai_smoke|baseline. service_venv: venv root holding lib/python*/site-packages/dftracer/bin/dftracer_service; default the venv of
        the moirai python recorded by the integrate stage (outputs moirai.python). It is verified here (the file exists and is executable): otherwise status error
        naming the fix, nothing is written. Pass the same value to moirai_smoke_run.
        data_dir: MOIRAI_DATA_DIR (default the annotated tree). original_script: the app's own submit script the wrapper runs
        (default <annotated>/monomer-structure-prediction/submit_smsp.sh).
        Returns {status, path, sha256, action: created|unchanged|rewritten|mode_fixed, previous_sha256, raw_dir, original_script, service_venv, dftracer_service, warnings} or {status: error, error}."""
        if stage not in STAGES:
            return {"status": "error", "error": f"stage must be one of {STAGES}"}
        try:
            _, r = _plan(pipeline, stage, "", "pdebug", 2400, data_dir, service_venv, original_script, strict=True)
            w = sync_wrapper(r["stage_dir"], r["wrapper_script"])
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}
        return {"status": "ok", "path": w["path"], "sha256": w["sha256"], "action": w["action"], "previous_sha256": w["previous_sha256"], "raw_dir": w["raw_dir"],
                "original_script": r["original_script"], "service_venv": r["service_venv"], "service_venv_source": r["service_venv_source"],
                "dftracer_service": r["resolver"]["path"], "warnings": r["warnings"]}

    @mcp.tool()
    def moirai_wrapper_selftest() -> dict:
        """Self-test of the wrapper tools on a temp dir (no pipeline, nothing on the cluster): create, sha256, mode 755, dirs, idempotence, detection and rewrite
        of a differing file, a changed render, mode repair, a run.sh not referencing the wrapper is refused, the dftracer_service resolver on a venv whose
        moirai-service is a Python script, and the post-batch verification run with bash on the real failed run.out, a clean one, and missing/empty hosts.txt.
        Use after any change to the wrapper text, the resolver or the batch wrapper job.
        Returns {status, line, cases}: line is 'moirai wrapper selftest PASS ...' or 'moirai wrapper selftest FAIL <case>'."""
        return _selftest()

    @mcp.tool()
    def moirai_batch_verify_test(run_out: str, hosts: str = "ok", out_present: bool = True) -> dict:
        """Run the batch wrapper job's post-batch verification (the same bash snippet it embeds) with bash on temp files. Nothing on the cluster. Use to see what the
        batch wrapper job would answer for a given batch --output text: rc 0 pass, 20 a chunk job failed (line 'exited with exit code <non-zero>' or ending ' failed'),
        21 hosts.txt missing, 22 hosts.txt empty, 23 the --output file missing.
        run_out: text of the batch --output file. hosts: ok|empty|missing. out_present: false = the --output file does not exist.
        Returns {rc, stderr, chunk_job_id}."""
        return batch_verify_run(run_out, hosts, out_present)

    @mcp.tool()
    def moirai_smoke_run(pipeline: str, command: str = "", launcher: str = "", wait: int = 600, limit: int = 1800, batch_script: str = "", queue: str = "pdebug",
                         data_dir: str = "", service_venv: str = "", original_script: str = "") -> dict:
        """Run the annotated app on the small test under moirai-service. Use after integrate. It stops the service however the app ends; limit: seconds before it is
        stopped. The Dashboard tab follows it live. batch_script: path of the generated flux-batch run.sh (Elmerfold), generated WITH --submit-script <wrapper path>
        from moirai_wrapper_write; run.sh is not modified: the wrapper <work>/traces/<stage>/submit_moirai.sh is compared with the freshly rendered text
        (rewritten, mode 755, and reported when it differs) and a detached job runs `flux batch -t <limit/60> --queue <queue> <run.sh>`, waits, then FAILS (rc 20-23)
        when the batch --output file shows a failed chunk job or hosts.txt is missing or empty (the batch return code alone does not show a failed bulksubmit chunk);
        a run.sh that does not reference the wrapper is refused before anything is written. Run moirai_batch_plan first and get the user's yes. queue: the batch queue.
        data_dir: MOIRAI_DATA_DIR (default the annotated tree). service_venv: venv root holding dftracer/bin/dftracer_service (default the integrate stage's moirai python
        venv; verified before anything is submitted, error naming the fix when missing). original_script: the app's own submit
        script the wrapper runs (default <annotated>/monomer-structure-prediction/submit_smsp.sh). Returns {status, job, rc, elapsed, traces, wrapper: {action, sha256,
        previous_sha256}, log_tail}."""
        if batch_script:
            return _start_batch(pipeline, "moirai_smoke", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        return _start(pipeline, "moirai_smoke", command, launcher, 1, "", wait, limit)

    @mcp.tool()
    def moirai_baseline_run(pipeline: str, command: str = "", launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = 3600,
                            batch_script: str = "", queue: str = "pdebug", service_venv: str = "", original_script: str = "") -> dict:
        """Start the real baseline run under moirai-service, in the background, on what the user approved at the gate. command: the full run. launcher: e.g. 'flux run -N4 -n32'.
        nodes: one service starts per node. data_dir: where its I/O is traced. limit: seconds before it is stopped. batch_script, queue, service_venv, original_script: as in
        moirai_smoke_run (batch mode ignores command, launcher, nodes: the run.sh decides; the wrapper on disk is verified against the fresh render and rewritten,
        with a report, when it differs; the batch wrapper job fails with rc 20-23 on a failed chunk job or empty hosts.txt). The Dashboard tab shows events as they appear.
        Returns {status, job, traces, wrapper}: watch with moirai_run_status."""
        if batch_script:
            return _start_batch(pipeline, "baseline", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        if not command:
            raise P.PipelineError("give command (with launcher) or batch_script")
        return _start(pipeline, "baseline", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_batch_plan(pipeline: str, stage: str, batch_script: str = "", queue: str = "pdebug", limit: int = 2400, data_dir: str = "", service_venv: str = "",
                          original_script: str = "") -> dict:
        """DRY RUN of the batch mode: nothing is written, submitted or started. Returns the text of the per-chunk submit wrapper, its path and whether the file on disk
        is missing, in sync or differs (wrapper_on_disk). The wrapper itself is written by moirai_wrapper_write, which must run BEFORE elmerfold_run_script (the
        generator needs an existing --submit-script). Order: moirai_wrapper_write, elmerfold_run_script with submit_script (and cwd), elmerfold_run_check with
        submit_script, this tool with batch_script, the user's yes, moirai_smoke_run. Once batch_script (the generated run.sh) is given it checks run.sh (one bulksubmit
        line, its ':::' input list, the wrapper path and the annotated tree in it) and prints the exact submit command, the batch wrapper job text and the batch
        --output file it will read (batch_output). service_venv defaults to the integrate stage's moirai python venv; resolver shows whether dftracer_service was found.
        stage: moirai_smoke|baseline. limit: seconds (the batch gets ceil(limit/60) minutes). original_script: default the annotated copy's submit_smsp.sh.
        Returns {status, wrapper_path, wrapper_script, wrapper_on_disk, batch_wrapper, batch_output, run_sh_check, submit_command, resolver, service_venv, warnings} or {status: error, error}."""
        try:
            _, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, service_venv, original_script)
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}
        r["wrapper_on_disk"] = wrapper_state(r["stage_dir"], r["wrapper_script"])
        if not r["resolver"]["path"]:
            r["warnings"].append("dftracer_service not found in the service venv: moirai_wrapper_write and moirai_smoke_run will refuse; pass service_venv")
        if not batch_script:
            r["submit_command"] = r["submit_command"].replace(r["run_script_path"], "<run.sh>")
            r["run_script_path"] = ""
            r["note"] = (f"run.sh not given: write the wrapper with moirai_wrapper_write (on disk: {r['wrapper_on_disk']['state']}), generate run.sh with "
                         f"elmerfold_run_script --submit-script {r['wrapper_path']} (the generator requires it to exist), then plan again with batch_script")
        elif r["wrapper_on_disk"]["state"] != "in_sync":
            r["warnings"].append(f"wrapper on disk is {r['wrapper_on_disk']['state']}: run moirai_wrapper_write (the run would rewrite it and say so)")
        r["warnings"] += ["UNCONFIRMED: whether `moirai-service start` returns by itself (the wrapper backgrounds it and waits for the ring either way)",
                          "UNCONFIRMED: submit_smsp.sh puts ${ELMER_ENV}/lib first in LD_LIBRARY_PATH for the app (libstdc++ shadowing of moirai/dftracer libs)",
                          "UNCONFIRMED: trace file names per host; whether the GPU server runs on the same node as the driver"]
        r.setdefault("note", "dry run: nothing started or written; the real run verifies the wrapper (mode 755, rewritten when it differs), then submits the batch wrapper job, which runs the submit command")
        return r

    @mcp.tool()
    def moirai_batch_render(original: str, env: str, stage_dir: str, data_dir: str = "/data", shm_base: str = "/moirai-agents-test", queue: str = "pdebug", limit: int = 2400,
                            run_sh: str = "", run_script: str = "", annotated: str = "", service_venv: str = "") -> dict:
        """Pure text render, no pipeline or files needed (sandbox checks, experiments). original: path of the app's own submit script; env: moirai_env.sh path;
        stage_dir: <work>/traces/<stage>; shm_base: ring name prefix (the job id is appended at run time). Optional run_sh: text of a generated run.sh to check against
        the wrapper (status error when it does not run the wrapper, has no ':::' or does not hold `annotated`); its `#flux: --output <path>` line is the batch log the
        batch wrapper job reads to fail on a failed chunk job. Returns {status, wrapper_path, wrapper_script, batch_wrapper, batch_output, submit_command, run_sh_check, error?}."""
        return render_batch(original, env, stage_dir, data_dir, shm_base, queue, limit, run_sh, run_script, annotated, service_venv)

    @mcp.tool()
    def moirai_service_resolve(binary: str = "", venv: str = "") -> dict:
        """Find the real dftracer_service for moirai-service, whose --help shows a build-time /tmp/... default. moirai-service in a venv is a Python entry-point script
        (`#!<venv>/bin/python`), so the answer is <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service, never a path relative to the script. binary: path of
        moirai-service (default: on PATH; its shebang names the venv when venv is empty). venv: venv root to search first.
        Returns {binary, is_script, shebang_venv, embedded_relative_path, tried, path, exists}; export MOIRAI_DFTRACER_SERVICE=<path> when the default is missing."""
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
                "raise limit if it timed out; rc 20 = a chunk job failed in the batch log, 21/22 = hosts.txt missing/empty, 23 = batch --output missing (see the wrapper line); otherwise compare with the smoke stage: did the annotation break it? Batch mode: read the batch --output file, the chunk logs (moirai-wrapper: lines) and service-<host>-<job>.log")
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
