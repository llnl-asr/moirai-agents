"""Run pipeline stages 5 and 6: run the annotated app under moirai-service, first a smoke run, then the baseline, watched live on the Dashboard.
Two shapes: a plain command (one launcher), or a generated `flux batch` run.sh (batch mode): run.sh stays UNMODIFIED; its app job runs a generated
per-chunk submit wrapper (--submit-script) that starts moirai-service inside the job's own shell. The wrapper is written beforehand by moirai_wrapper_write.
Batch mode submits the flux batch DETACHED (own session, nohup, clean environment, no cancel on any signal) and records the flux job id; state and the
post-run evaluation come from flux and run.out (moirai_run_status / moirai_validate), never from a process of the tool server. Only moirai_run_stop cancels.
A third entry: a flux batch submitted by hand and already finished (BioTox) is attached with moirai_run_attach so that moirai_validate can judge it."""
import glob
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGES = ("moirai_smoke", "baseline", "live_run")
LIVE_LIMIT_S = 12 * 3600      # a live run goes for hours: its default limit
MARKER = "moirai preamble (generated)"
DFT_REL = "../../dftracer/bin/dftracer_service"
DFT_GLOB = ("lib", "python*", "site-packages", "dftracer", "bin", "dftracer_service")
WRAPPER_NAME = "submit_moirai.sh"
ORIGINAL_REL = "monomer-structure-prediction/submit_smsp.sh"   # Elmerfold's per-chunk submit script, under ELMER_SRC_DIR
FLUX_JOBS_FORMAT = "{state} {result} {returncode} {runtime}"
FLUX_ID_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")
ATTACH_LOG_TAIL = 20000
TRACE_HOST_RE = re.compile(r"^prov[_-](?P<host>.+?)(?:-spill)?\.pfw(?:\.gz)?$")
BATCH_SOURCE = "flux-batch"
FLUX_QUEUED = ("NEW", "DEPEND", "PRIORITY", "SCHED")
OUTPUT_SCAN_BYTES = 50 * 1024 * 1024      # the tail of run.out that the post-run evaluation reads


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
def _binary_kind(path: str) -> str:
    """'python-entry-script' (shebang with python: the pip entry point), 'binary' (anything else readable) or '' (missing/unreadable)."""
    try:
        with open(os.path.realpath(path), "rb") as f:
            head = f.read(256)
    except OSError:
        return ""
    if not head:
        return ""
    first = head.split(b"\n", 1)[0]
    return "python-entry-script" if first.startswith(b"#!") and b"python" in first else "binary"


def resolve_dftracer_service(binary: str = "", venv: str = "") -> dict:
    """Where the real dftracer_service is. The installed package is the truth: <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service.
    The venv's `moirai-service` is a Python entry-point script and says nothing about it; only a COMPILED moirai-service holding the string
    ../../dftracer/bin/dftracer_service is also tried, relative to its real path (last)."""
    b = binary or shutil.which("moirai-service") or ""
    tried, found, embedded, kind = [], "", False, ""
    if venv:
        tried += sorted(glob.glob(os.path.join(os.path.expanduser(venv), *DFT_GLOB)))
    if b and os.path.isfile(b):
        kind = _binary_kind(b)
        real = os.path.realpath(b)
        if kind == "binary":
            try:
                embedded = DFT_REL.encode() in Path(real).read_bytes()
            except OSError:
                embedded = False
            if embedded:
                tried.append(os.path.normpath(os.path.join(os.path.dirname(real), DFT_REL)))
    for c in tried:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            found = os.path.realpath(c)
            break
    return {"binary": b, "binary_kind": kind, "embedded_relative_path": embedded, "tried": tried, "path": found}


def default_service_venv(integ: dict) -> str:
    """Pure: the venv root of the integrate stage's moirai.python (<venv>/bin/python -> <venv>); '' when the integrate outputs hold none.
    Accepts the flat key 'moirai.python' or a nested {'moirai': {'python': ...}}."""
    nested = integ.get("moirai")
    py = integ.get("moirai.python") or (nested.get("python") if isinstance(nested, dict) else "") or ""
    if not isinstance(py, str) or not py:
        return ""
    return str(Path(py).parent.parent).rstrip("/")


def check_service_venv(venv: str) -> dict:
    """<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service must exist and be executable; raises P.PipelineError naming the fix when not."""
    res = resolve_dftracer_service("", venv)
    if not res["path"]:
        raise P.PipelineError(
            f"service venv {venv} holds no executable dftracer_service (looked for {os.path.join(venv, 'lib', 'python*', 'site-packages', 'dftracer', 'bin', 'dftracer_service')}): "
            f"pass service_venv of the venv where dftracer is installed (the root of integrate's moirai.python), or install dftracer there; "
            f"moirai_service_resolve(venv=...) shows what was tried")
    return res


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
# an explicit MOIRAI_DFTRACER_SERVICE wins. Otherwise: moirai-service --help prints a build-time default; when it does not exist, use the installed package
# (resolved through python, whose PATH entry after sourcing the env file is the venv's shim; moirai-service itself is only a python entry script)
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  _dflt="$(moirai-service --help 2>&1 | grep -o '/[^[:space:]]*dftracer_service' | head -1)"
  if [ -z "${_dflt}" ] || [ ! -x "${_dflt}" ]; then
    _pkg="$(python -c 'import dftracer,os;print(os.path.dirname(dftracer.__file__))' 2>/dev/null)"
    for _c in __RESOLVED__${_pkg:+"${_pkg}/bin/dftracer_service"}__VENVGLOB__; do
      if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
    done
    [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service found (default ${_dflt:-unknown} missing, dftracer package ${_pkg:-not importable})" >&2; exit 2; }
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
    """Pure: the path in run.sh's `#flux: --output <path>` header line ('' when there is none)."""
    m = re.search(r"^#flux:\s*--output(?:=|\s+)(\S+)", run_sh or "", re.M)
    return m.group(1).strip("'\"") if m else ""


# ---------------------------------------------------------------- the detached batch submit script (pure text)
def _wrapper(run_script, minutes, queue, modules, name, idfile):
    """The text of the SUBMIT job: load modules, check dbcast, `flux batch`, write the flux job id to `idfile`, exit. It does not wait, does not watch and never
    cancels: the Flux job belongs to Flux, and its state and the post-run evaluation are read later from flux and run.out by finish_batch."""
    q = shlex.quote
    mods = " ".join(q(m) for m in modules)
    return f"""set -uo pipefail
# This script only submits. It never cancels the flux job (not on a signal either): only moirai_run_stop does, with an explicit cancel of the recorded id.
# After the job ends, moirai_run_status / moirai_validate evaluate it from flux and the run.sh --output file (a chunk line 'exited with exit code [1-9][0-9]*'
# or ending ' failed' means a chunk job failed although the batch returned 0; no host in hosts.txt means no wrapper ran).
if [ -n {q(mods)} ]; then
  type module >/dev/null 2>&1 || {{ echo "wrapper: the 'module' command is not defined in this shell; cannot load {mods}" >&2; exit 1; }}
  module load {mods} || {{ echo "wrapper: module load {mods} failed" >&2; exit 1; }}
fi
command -v dbcast >/dev/null 2>&1 || {{ echo "wrapper: dbcast not on PATH after module load; flux batch would propagate a shell without it" >&2; exit 1; }}
jid="$(flux batch -t {minutes} --queue {q(queue)} {q(run_script)})" || {{ echo "wrapper: flux batch submission failed" >&2; exit 1; }}
case "$jid" in ''|*[!A-Za-z0-9]*) echo "wrapper: flux batch printed no usable job id: '$jid'" >&2; exit 1 ;; esac
echo "batch job: $jid"
mkdir -p "$(dirname {q(idfile)})" && printf '%s\\n' "$jid" > {q(idfile)}.tmp && mv {q(idfile)}.tmp {q(idfile)} || {{ echo "wrapper: cannot record the job id $jid in {idfile}" >&2; exit 1; }}
echo "wrapper: batch job $jid submitted and recorded in {idfile}; its state is read from flux (flux jobs $jid), not from this script"
exit 0
"""


def render_batch(original, env, stage_dir, data_dir="", shm_base="/moirai-agents-run", queue="pdebug", limit=2400, run_sh="", run_script="",
                 annotated="", venv="", dft_service="", modules=("mpifileutils/0.11.1",), wait_s=60, stage="moirai_smoke") -> dict:
    """Pure: wrapper text, batch submit script text, exact submit command, and (when run_sh text is given) the run.sh check. Never touches the cluster."""
    sd = str(stage_dir).rstrip("/")
    wpath = f"{sd}/{WRAPPER_NAME}"
    minutes = max(1, math.ceil(int(limit) / 60))
    rs = run_script or f"{str(Path(sd).parent)}/run.sh"
    submit = f"flux batch -t {minutes} --queue {shlex.quote(queue)} {shlex.quote(rs)}"
    out_path = batch_output_path(run_sh)
    idfile = f"{sd}/flux_job_id"
    r = {"status": "ok", "wrapper_path": wpath, "wrapper_script": render_submit_wrapper(original, env, sd, data_dir, shm_base, wait_s, venv, dft_service),
         "batch_wrapper": _wrapper(rs, minutes, queue, list(modules), stage, idfile), "submit_command": submit, "minutes": minutes,
         "run_script_path": rs, "original_script": original, "hosts_file": f"{sd}/hosts.txt", "traces": f"{sd}/raw", "batch_output": out_path,
         "flux_job_id_file": idfile, "warnings": []}
    if not venv and not dft_service:
        r["warnings"].append("no service venv given and dftracer_service not resolved here: the wrapper can only find it through python's dftracer package on PATH")
    if run_sh and not out_path:
        r["warnings"].append("run.sh has no '#flux: --output <path>' header: the post-run evaluation cannot read the chunk jobs' output, it only checks hosts.txt")
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
    integ = P.stage(P.origin(c), "integrate")["outputs"]      # a live run uses its parent's annotated tree
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
    venv = (venv or default_service_venv(integ)).rstrip("/")     # explicit and defaulted give the same text
    res = check_service_venv(venv) if venv else resolve_dftracer_service("", "")
    r = render_batch(original, integ["env"], str(sdir), data_dir or integ["annotated"], shm, queue, limit, run_sh, rs, integ["annotated"], venv, res["path"], stage=stage)
    r["resolver"] = res
    r["service_venv"] = venv
    r["stage_dir"], r["annotated"] = str(sdir), integ["annotated"]
    return c, r


# ---------------------------------------------------------------- detached submit, state from flux, post-run evaluation, explicit stop
def detached_env() -> dict:
    """Pure-ish: the clean environment the detached submit gets (HOME, USER, LOGNAME, TERM from this process; PATH only so that nohup/env are found)."""
    e = {"PATH": "/usr/bin:/bin"}
    for k in ("HOME", "USER", "LOGNAME"):
        if os.environ.get(k):
            e[k] = os.environ[k]
    e["TERM"] = os.environ.get("TERM") or "dumb"
    return e


def detached_argv(sh, subrc, env_pass=None) -> list:
    """Pure: argv of the detached submit: nohup env -i HOME USER TERM bash -lc 'bash <sh>; echo $? > <subrc>' (a clean login shell, so `module` exists and
    flux/PATH come from the login profile, not from the tool server). The caller starts it in its own session with stdin /dev/null and the output in the job log."""
    q = shlex.quote
    env_pass = env_pass if env_pass is not None else detached_env()
    pairs = [f"{k}={v}" for k, v in env_pass.items() if k != "PATH"]
    return ["nohup", "env", "-i", *pairs, "bash", "-lc", f"bash {q(str(sh))}; echo $? > {q(str(subrc))}"]


def start_detached(pipeline: str, stage: str, name: str, script: str, cwd: str, limit: int, extra: dict) -> dict:
    """Write the submit script and start it detached from this process (own session via setsid(2), nohup, clean env, stdin /dev/null, output to the job log).
    Writes the job record <pipeline>/jobs/<id>.json with source 'flux-batch'; the flux job id is added as soon as the script records it. Returns the record."""
    d = P.pdir(pipeline) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    jid = f"{re.sub(r'[^a-z0-9_]+', '-', name.lower())}-{int(time.time() * 10) % 10**9}"
    sh, log, rcf, subrc = d / f"{jid}.sh", d / f"{jid}.log", d / f"{jid}.rc", d / f"{jid}.submit.rc"
    sh.write_text(script)
    env_pass = detached_env()
    with open(log, "ab") as lf:
        p = subprocess.Popen(detached_argv(sh, subrc, env_pass), cwd=cwd, env=env_pass, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
    rec = {"id": jid, "pipeline": pipeline, "stage": stage, "name": name, "pid": p.pid, "cwd": cwd, "started": round(time.time(), 1), "limit_s": int(limit),
           "script": str(sh), "log": str(log), "rc_file": str(rcf), "submit_rc_file": str(subrc), "source": BATCH_SOURCE, "flux_job_id": "", **extra}
    (d / f"{jid}.json").write_text(json.dumps(rec, indent=1))
    return rec


def _read(p) -> str:
    try:
        return Path(str(p)).read_text().strip() if p else ""
    except OSError:
        return ""


def evaluate_batch(row: dict, jid: str, out_path: str, hosts_file: str):
    """Pure-ish (reads out_path and hosts_file): the verdict on a FINISHED flux batch, from `flux jobs` fields and the run.sh --output file, because the batch
    return code does not reflect its bulksubmit chunk jobs. Returns (rc, message lines): flux's own non-zero returncode (124 for TIMEOUT, 1 when none) when the
    batch itself failed; 6 --output unreadable; 4 a chunk job failed ('exited with exit code <non-zero>' or a line ending ' failed'); 5 no host in hosts.txt; else 0."""
    res, rcs = row.get("result", ""), str(row.get("returncode", ""))
    if res != "COMPLETED" or rcs != "0":
        rc = 124 if res == "TIMEOUT" else (int(rcs) if rcs.isdigit() and int(rcs) > 0 else 1)
        return rc, [f"batch job {jid} result={res} returncode={rcs}",
                    f"wrapper: batch job {jid} FAILED (result={res} returncode={rcs}); see its --output file, the chunk logs and the service logs next to the traces"]
    if out_path:
        try:
            with open(out_path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - OUTPUT_SCAN_BYTES))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return 6, [f"wrapper: batch job {jid} ended {res} but its --output file {out_path} is not readable: cannot tell whether the chunk jobs ran"]
        failed = [l for l in lines if re.search(r" failed\s*$", l)][:5]
        codes = [l for l in lines if re.search(r"exited with exit code [1-9][0-9]*", l)][:5]
        if failed or codes:
            ids = " ".join(m.group(1) for m in (re.match(r"^.*: (\S+) failed\s*$", l) for l in failed) if m)
            return 4, [f"wrapper: batch job {jid} returned 0 but a chunk job FAILED (chunk job id(s): {ids or 'unknown'}); run.sh ends normally even when its "
                       f"bulksubmit chunk job fails. Evidence in {out_path}:", *codes, *failed]
    hosts = _read(hosts_file)
    if not hosts:
        return 5, [f"wrapper: batch job {jid} left no host in {hosts_file}: no per-chunk wrapper ran, so no service was started and nothing was traced"]
    uniq = " ".join(sorted(set(hosts.split())))
    return 0, [f"wrapper: batch job {jid} clean: no failed chunk job in {out_path or '<no --output header>'}; hosts: {uniq}"]


def finish_batch(rec: dict, flux: str = "flux", tries: int = 2, pause: float = 0.5) -> dict:
    """The state of a detached flux batch job record, derived from FLUX and files, never from a server process. Records the flux job id in the record as soon as
    the submit script wrote it. When flux says INACTIVE with a result and no rc file exists yet, runs evaluate_batch and writes the rc file and the verdict lines
    into the job log (so it also happens after a restart of the server, by whoever asks first). Returns {state, flux_job_id, flux_state, flux_result,
    flux_returncode, flux_runtime, rc, message}. state: submitting | queued | running | completed | failed | cancelled | timeout | lost | unreachable.
    'lost' only when flux answers that it does not know the id (or the submit ended without recording one); 'unreachable' when flux itself cannot be run."""
    rcf, logf = Path(rec["rc_file"]), Path(rec["log"])
    jpath = Path(rec["script"]).with_suffix(".json")
    fid = rec.get("flux_job_id") or _read(rec.get("idfile"))
    if fid and fid != rec.get("flux_job_id"):
        rec["flux_job_id"] = fid
        try:
            jpath.write_text(json.dumps(rec, indent=1))
        except OSError:
            pass
    out = {"state": "", "flux_job_id": fid, "flux_state": "", "flux_result": "", "flux_returncode": "", "flux_runtime": "", "rc": None, "message": ""}
    done = _read(rcf)
    if done.isdigit():
        out["rc"] = int(done)
        out["state"] = "completed" if out["rc"] == 0 else ("timeout" if out["rc"] == 124 else "failed")
        out["message"] = "already evaluated"
        if rec.get("flux_result") == "CANCELED":
            out["state"] = "cancelled"
        return out

    def write_rc(rc, lines):
        with open(logf, "a") as fh:
            fh.write("\n".join(lines) + f"\nbatch job rc={rc}\n")
        rcf.write_text(f"{rc}\n")

    if not fid:
        sub = _read(rec.get("submit_rc_file"))
        if sub == "" and J._alive(int(rec.get("pid") or 0)):
            out["state"], out["message"] = "submitting", "the detached submit is still running; no flux job id recorded yet"
        elif sub.isdigit() and int(sub) != 0 or sub == "0":
            rc = int(sub) if sub.isdigit() and int(sub) > 0 else 1
            write_rc(rc, [f"wrapper: the submit ended (rc {sub}) without recording a flux job id: nothing was submitted; read the lines above"])
            out["rc"], out["state"], out["message"] = rc, "failed", "submit failed before flux printed a job id"
        else:
            out["state"], out["message"] = "lost", "the submit process is gone and no flux job id nor submit rc was recorded"
        return out
    try:
        row = flux_status(fid, flux, tries, pause)
    except P.PipelineError as e:
        out["state"] = "unreachable" if "command not found" in str(e) else "lost"
        out["message"] = str(e)
        return out
    out.update(flux_state=row["state"], flux_result=row["result"], flux_returncode=row["returncode"], flux_runtime=row["runtime"])
    if row["state"] == "INACTIVE" and row["result"] not in ("", "-"):
        rc, lines = evaluate_batch(row, fid, rec.get("batch_output", ""), rec.get("hosts_file", ""))
        write_rc(rc, lines)
        rec["flux_result"] = row["result"]
        try:
            jpath.write_text(json.dumps(rec, indent=1))
        except OSError:
            pass
        out["rc"] = rc
        out["state"] = "cancelled" if row["result"] == "CANCELED" else "timeout" if row["result"] == "TIMEOUT" else "completed" if rc == 0 else "failed"
        out["message"] = lines[-1]
    elif row["state"] in FLUX_QUEUED:
        out["state"], out["message"] = "queued", f"flux job {fid} is waiting in the queue ({row['state']})"
    else:
        out["state"], out["message"] = "running", f"flux job {fid} state {row['state']}"
    return out


def stop_batch(rec: dict, flux: str = "flux") -> dict:
    """The ONLY way a detached batch is cancelled: `flux cancel <recorded id>` when flux says the job is not INACTIVE. Returns {status, id, stopped, flux_job_id, note}."""
    fid = rec.get("flux_job_id") or _read(rec.get("idfile"))
    if not fid:
        return {"status": "ok", "id": rec["id"], "stopped": False, "flux_job_id": "",
                "note": "no flux job id is recorded yet (the submit is still running or failed): nothing was cancelled; ask again in a few seconds"}
    try:
        row = flux_status(fid, flux, 2, 0.5)
    except P.PipelineError as e:
        return {"status": "error", "id": rec["id"], "stopped": False, "flux_job_id": fid, "error": str(e)}
    if row["state"] == "INACTIVE":
        return {"status": "ok", "id": rec["id"], "stopped": False, "flux_job_id": fid, "note": f"flux job {fid} already ended ({row['result']})"}
    try:
        p = subprocess.run([flux, "cancel", fid], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "error", "id": rec["id"], "stopped": False, "flux_job_id": fid, "error": f"flux cancel {fid} failed: {e}"}
    if p.returncode != 0:
        return {"status": "error", "id": rec["id"], "stopped": False, "flux_job_id": fid, "error": f"flux cancel {fid}: {(p.stderr or p.stdout).strip()[-200:]}"}
    return {"status": "ok", "id": rec["id"], "stopped": True, "flux_job_id": fid, "note": f"flux cancel {fid} sent; moirai_run_status shows it as cancelled once flux has ended it"}


def _load_rec(pipeline: str, jid: str) -> dict:
    p = P.pdir(pipeline) / "jobs" / f"{jid}.json"
    if not p.exists():
        raise P.PipelineError(f"no job {jid} in {pipeline}")
    return json.loads(p.read_text())


def _sync_stage(pipeline: str, rec: dict, fb: dict) -> None:
    """Write the flux job id and flux state into the stage outputs when they changed (only for the stage's current job)."""
    try:
        o = P.stage(P.load(pipeline), rec["stage"])["outputs"]
        if o.get("job") != rec["id"]:
            return
        key = (fb["flux_job_id"], fb["state"])
        if (o.get("flux_job_id"), o.get("flux_state")) != key:
            P.record(pipeline, rec["stage"], outputs={"flux_job_id": fb["flux_job_id"], "flux_state": fb["state"]},
                     note=f"flux job {fb['flux_job_id'] or '-'}: {fb['state']}" + (f" ({fb['message']})" if fb["message"] and fb["state"] in ("lost", "unreachable", "failed") else ""))
    except (P.PipelineError, OSError, KeyError):
        pass


def batch_job(pipeline: str, jid: str, tail: int = 2000, flux: str = "flux", tries: int = 2, pause: float = 0.5) -> dict:
    """J.get for any job, but a detached flux batch (source 'flux-batch') is first brought up to date from flux (finish_batch) and its status is the real one:
    running (submitting, queued or running in flux), done (evaluated, rc set), lost (flux does not know the id) or unknown (flux not reachable). Adds flux_*."""
    rec = _load_rec(pipeline, jid)
    if rec.get("source") != BATCH_SOURCE:
        return J.get(pipeline, jid, tail)
    fb = finish_batch(rec, flux, tries, pause)
    j = J.get(pipeline, jid, tail)
    if j["status"] != "done":
        j["status"] = "lost" if fb["state"] == "lost" else "unknown" if fb["state"] == "unreachable" else "running"
    j.update(flux_job_id=fb["flux_job_id"], flux_state=fb["state"], flux_detail=fb)
    _sync_stage(pipeline, rec, fb)
    return j


def _start_batch(pipeline, stage, batch_script, queue, limit, data_dir, venv, wait, original):
    if not batch_script:
        raise P.PipelineError("give batch_script (the generated run.sh)")
    c, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, venv, original)
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
    Path(r["flux_job_id_file"]).unlink(missing_ok=True)
    extra = {"idfile": r["flux_job_id_file"], "batch_output": r["batch_output"], "hosts_file": r["hosts_file"], "queue": queue, "submit_command": r["submit_command"]}
    j = start_detached(pipeline, stage, stage.replace("_", "-") + "-batch", r["batch_wrapper"], r["annotated"], int(limit), extra)
    P.set_dashboard(pipeline, r["traces"], live=True)
    P.record(pipeline, stage, status="running", outputs={"job": j["id"], "traces": r["traces"], "batch_script": str(batch_script), "run_script": r["run_script_path"],
                                                        "wrapper": r["wrapper_path"], "wrapper_sha256": wr["sha256"], "hosts_file": r["hosts_file"],
                                                        "submit_command": r["submit_command"], "queue": queue, "mode": "batch", "batch_output": r["batch_output"],
                                                        "service_venv": r["service_venv"], "flux_job_id": "", "flux_state": "submitting"},
             note=f"submitted DETACHED as a flux batch (independent of the server): {r['submit_command']}{note_w}")
    end = time.time() + max(0, int(wait))
    while True:                         # wait only for the id to be recorded; the job itself is not waited for
        w = batch_job(pipeline, j["id"], 2500)
        if w["flux_job_id"] or w["status"] in ("done", "lost", "unknown") or time.time() >= end:
            break
        time.sleep(0.5)
    return {"status": w["status"], "job": j["id"], "rc": w["rc"], "elapsed": w["elapsed"], "traces": r["traces"], "submit_command": r["submit_command"],
            "flux_job_id": w["flux_job_id"], "flux_state": w["flux_state"], "wrapper": wr, "log_tail": w.get("log_tail", "")[-2500:],
            "note": "the flux batch is detached: a restart of the server does not cancel it; follow it with moirai_run_status, cancel it only with moirai_run_stop. "
                    "The Dashboard tab shows the provenance as it arrives" + note_w}


def _start(pipeline, stage, command, launcher, nodes, data_dir, wait, limit):
    c = P.load(pipeline)
    P.require_ready(c, stage)
    integ, cmd = P.stage(P.origin(c), "integrate")["outputs"], command or c["command"]
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


# ---------------------------------------------------------------- attach a finished, externally submitted flux batch
def parse_flux_row(text: str):
    """Pure: the first non-empty line of `flux jobs -n -a -o '{state} {result} {returncode} {runtime}' <id>` as {state, result, returncode, runtime}
    (fields missing from a short line are ''); None when there is no line."""
    for line in (text or "").splitlines():
        t = line.split()
        if t:
            return {"state": t[0], "result": t[1] if len(t) > 1 else "", "returncode": t[2] if len(t) > 2 else "", "runtime": t[3] if len(t) > 3 else ""}
    return None


def derive_rc(row: dict, study_ok=None) -> int:
    """Pure: the return code of an attached job, from flux and never from the caller. 0 only for result COMPLETED with returncode 0 (and study_ok not False);
    otherwise flux's own positive returncode, else 1. study_ok: the 'ok' of check_study's result.json (None when none was given)."""
    rcs = str(row.get("returncode", ""))
    if row.get("result") == "COMPLETED" and rcs == "0":
        return 1 if study_ok is False else 0
    return int(rcs) if rcs.isdigit() and int(rcs) > 0 else 1


def flux_status(flux_job_id: str, flux: str = "flux", tries: int = 3, pause: float = 1.0) -> dict:
    """Ask flux for one job: {state, result, returncode, runtime}. Retried `tries` times (flux can answer late); raises P.PipelineError when the id is
    malformed, flux is missing, or the job stays unknown. Read-only: it runs `flux jobs` and nothing else."""
    if not FLUX_ID_RE.match(flux_job_id or ""):
        raise P.PipelineError(f"flux_job_id {flux_job_id!r} is not a flux job id (letters and digits, e.g. f3iKMXkX8ykB)")
    last = ""
    for i in range(max(1, int(tries))):
        try:
            p = subprocess.run([flux, "jobs", "-n", "-a", "-o", FLUX_JOBS_FORMAT, flux_job_id], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            last = (p.stderr or p.stdout or "").strip()[-200:]
            row = parse_flux_row(p.stdout) if p.returncode == 0 else None
            if row:
                return row
        except FileNotFoundError:
            raise P.PipelineError(f"{flux}: command not found: run from a shell where flux is on PATH")
        except (subprocess.TimeoutExpired, OSError) as e:
            last = str(e)
        if i + 1 < max(1, int(tries)) and pause:
            time.sleep(pause)
    raise P.PipelineError(f"flux job {flux_job_id} is unknown, or flux did not answer after {max(1, int(tries))} tries: {last or 'no output'}")


def trace_hosts(files) -> dict:
    """Pure: {host: [files]} read from trace file names prov_<host>[-spill].pfw[.gz] (the service trace of BioTox: prov_<host>.pfw.gz); other names are ignored."""
    out = {}
    for f in files:
        m = TRACE_HOST_RE.match(os.path.basename(f))
        if m:
            out.setdefault(m.group("host"), []).append(f)
    return out


def attach_job(pipeline: str, stage: str, flux_job_id: str, output_file: str = "", result_json: str = "", flux: str = "flux", pause: float = 1.0) -> dict:
    """Record a FINISHED flux job as the stage's job (source 'attached'), in the pipeline's own jobs dir and stage outputs. The rc is derived here from flux
    and result_json, never given. Raises P.PipelineError, having written nothing, when the stage is not moirai_smoke|baseline, the stage is not ready, a job of
    the stage is still running, the flux job is unknown or not INACTIVE, or result_json/output_file is unreadable."""
    if stage not in STAGES:
        raise P.PipelineError(f"stage must be one of {STAGES}")
    fid = (flux_job_id or "").strip()
    c = P.load(pipeline)
    P.require_ready(c, stage)
    o = P.stage(c, stage)["outputs"]
    if o.get("job"):
        try:
            prev = batch_job(pipeline, o["job"], 0, flux, 1, 0)
        except P.PipelineError:
            prev = {}
        if prev.get("status") == "running":
            raise P.PipelineError(f"job {o['job']} of stage {stage} is still running: moirai_run_stop it first; nothing was recorded")
    row = flux_status(fid, flux, 3, pause)
    if row["state"] != "INACTIVE":
        raise P.PipelineError(f"flux job {fid} is not finished (state {row['state']}): wait for it to end; nothing was recorded, nothing was cancelled")
    if not row["result"] or not row["returncode"]:
        raise P.PipelineError(f"flux job {fid} is INACTIVE but flux gave no result/returncode ({row}): try again; nothing was recorded")
    study_ok, problems = None, []
    if result_json:
        try:
            d = json.loads(Path(result_json).expanduser().read_text())
        except (OSError, ValueError) as e:
            raise P.PipelineError(f"result_json {result_json} is not readable JSON ({e}): pass check_study's result.json of the run dir, or leave it out")
        if not isinstance(d, dict) or not isinstance(d.get("ok"), bool):
            raise P.PipelineError(f"result_json {result_json} has no boolean 'ok': it is not a check_study result")
        study_ok = d["ok"]
        pr = d.get("problems") or []
        problems = [str(x) for x in pr] if isinstance(pr, list) else [str(pr)]
        if not study_ok and not problems:
            problems = ["check_study reported ok=false without listing problems"]
    tail = ""
    if output_file:
        try:
            with open(Path(output_file).expanduser(), "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - ATTACH_LOG_TAIL))
                tail = fh.read().decode("utf-8", "replace")
        except OSError as e:
            raise P.PipelineError(f"output_file {output_file} is not readable ({e}): pass the batch --output file, or leave it out")
    rc = derive_rc(row, study_ok)
    try:
        rt = max(0.0, float(row["runtime"]))
    except ValueError:
        rt = 0.0
    now = time.time()
    jd = P.pdir(pipeline) / "jobs"
    jd.mkdir(parents=True, exist_ok=True)
    jid = "attached-" + re.sub(r"[^a-z0-9_]+", "-", fid.lower())
    sh, log, rcf = jd / f"{jid}.sh", jd / f"{jid}.log", jd / f"{jid}.rc"
    lines = [f"attached flux job {fid}: state={row['state']} result={row['result']} returncode={row['returncode']} runtime={row['runtime']} s (read with flux jobs, not given)"]
    if output_file:
        lines += [f"--- last {ATTACH_LOG_TAIL} bytes of {output_file} ---", tail.rstrip("\n")]
    if result_json:
        lines.append(f"result_json {result_json}: ok={study_ok}" + ("".join(f"\n  problem: {x}" for x in problems)))
    lines.append(f"attached job rc={rc}" + ("" if rc == 0 else " (flux result/returncode, or result_json ok=false)"))
    sh.write_text(f"# attached: flux job {fid} was submitted outside moirai_agents; this pipeline ran no script for it\n")
    log.write_text("\n".join(lines) + "\n")
    rcf.write_text(f"{rc}\n")
    os.utime(rcf, (now, now))
    traces = str(P.work(c) / "traces" / stage / "raw")
    rec = {"id": jid, "pipeline": pipeline, "stage": stage, "name": f"{stage}-attached", "pid": 0, "cwd": str(P.work(c)), "started": round(now - rt, 1), "limit_s": 0,
           "script": str(sh), "log": str(log), "rc_file": str(rcf), "source": "attached", "flux_job_id": fid, "flux_state": row["state"], "flux_result": row["result"],
           "flux_returncode": row["returncode"], "output_file": output_file, "result_json": result_json}
    (jd / f"{jid}.json").write_text(json.dumps(rec, indent=1))
    n = len(T.expand(traces))
    P.record(pipeline, stage, status="running",
             outputs={"job": jid, "traces": traces, "mode": "attached", "flux_job_id": fid, "hosts_file": "", "batch_output": output_file, "result_json": result_json},
             note=f"attached finished flux job {fid} ({row['result']}, returncode {row['returncode']}, rc {rc}); {n} trace file(s) in {traces}")
    return {"status": "ok", "job": jid, "source": "attached", "flux_job_id": fid, "flux_state": row["state"], "flux_result": row["result"], "flux_returncode": row["returncode"],
            "rc": rc, "elapsed": round(rt, 1), "traces": traces, "trace_files": n, "result_json_ok": study_ok, "problems": problems,
            "warnings": ([] if n else [f"no trace file under {traces}: moirai_validate will fail trace; the traces of this run must be under that directory"]),
            "next": "moirai_validate(pipeline, stage)"}


# ---------------------------------------------------------------- validate
def _validate(pipeline: str, stage: str) -> dict:
    c = P.load(pipeline)
    o = P.stage(c, stage)["outputs"]
    v = P.Verdict(stage)
    if not v.check("ran", bool(o.get("job")), "the run has not started",
                   "start the run (moirai_smoke_run / moirai_baseline_run); for a flux batch submitted by hand that has FINISHED: moirai_run_attach(pipeline, stage, flux_job_id, output_file, result_json)"):
        return P.finish_validation(pipeline, v)
    j = batch_job(pipeline, o["job"], 1500)       # a detached flux batch is evaluated here from flux and run.out when it has ended (also after a server restart)
    if j["status"] in ("running", "unknown"):
        fx = f" (flux job {j['flux_job_id']}: {j['flux_state']})" if j.get("flux_job_id") else ""
        v.inconclusive("finished", f"{'still running' if j['status'] == 'running' else 'flux not reachable'} after {j['elapsed']} s{fx}")
        return P.finish_validation(pipeline, v)
    lost = f" flux does not know job {j.get('flux_job_id') or '?'}: {j.get('flux_detail', {}).get('message', '')};" if j["status"] == "lost" else ""
    v.check("exit_0", j["rc"] == 0, f"rc={j['rc']}{' (timed out)' if j.get('timed_out') else ''}:{lost} {j.get('log_tail', '')[-300:]}",
            "raise limit if it timed out; rc 4/5/6 in batch mode: a chunk job failed / no wrapper ran / --output unreadable, read the batch --output file, the chunk logs (moirai-wrapper: lines) and service-<host>-<job>.log; "
            "lost: `flux jobs -a <flux_job_id>` from the shell that owns the flux instance; "
            "attached: rc comes from flux result/returncode and result_json ok, the problems are at the end of the log")
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
    elif o.get("mode") == "attached" and files:
        # no wrapper hosts.txt (the run was submitted outside this tool, e.g. BioTox): hosts come only from the trace inventory (prov_<host>.pfw.gz); no names: nothing is judged
        th = trace_hosts(files)
        if th:
            spill = [h for h, fs in th.items() if all("-spill" in f for f in fs)]
            v.check("host_service_trace", not spill, f"only spill traces from {spill}", "the app there did not reach the ring: check /dev/shm/<MOIRAI_SERVICE_SHM> on that host and the service log")
            v.assume("attached_hosts", True, f"no wrapper hosts.txt (attached run): {len(th)} host(s) with a service trace by file name: {sorted(th)}")
            P.record(pipeline, stage, outputs={"per_host_traces": {h: len(fs) for h, fs in th.items()}})
    g = T.read(o["traces"])
    n = g.counts()
    v.check("activities", n["activities"] > 0, f"{n['activities']} activities", "is the annotated code on the executed path? Is the app run from the annotated tree?")
    v.check("entities", n["entities"] > 0, f"{n['entities']} entities", "activities relate no entity: check the spec")
    v.check("no_dangling", n["dangling"] == 0, f"{n['dangling']} ids with no EH record", "every related entity must be declared")
    spec = P.stage(P.origin(c), "annotate")["outputs"].get("spec") or {}
    seen = {e["type"] for e in g.entities.values()}
    missing = sorted({e["type"] for a in spec.get("activities", []) for e in a.get("used", []) + a.get("generated", [])} - seen)
    if stage == "live_run":      # a live run is judged on what it ran; the spec's coverage was proven by the parent's baseline and graph diff
        v.check("types_seen", True, f"never recorded in this run (information, not a failure): {missing}" if missing else "every spec type recorded")
    else:
        v.check("types_seen", not missing, f"never recorded: {missing}", "that code path did not run: use a test input that reaches it, or fix the spec")
    P.record(pipeline, stage, outputs={"counts": n, "summary": f"{n['activities']} activities, {n['entities']} entities, {n['edges']} edges"})
    return P.finish_validation(pipeline, v)


_FAKE_FLUX = """#!/bin/bash
# fake flux for the detached batch: `batch` prints an id; `jobs` prints the line in ./state for that id (unknown otherwise); `cancel` is logged in ./cancelled
d="$(dirname "$0")"
case "$1" in
  batch) echo fFAKEBATCH ;;
  jobs) case "${@: -1}" in fFAKEBATCH) cat "$d/state" ;; *) echo "flux-jobs: error: unknown job ${@: -1}" >&2; exit 1 ;; esac ;;
  cancel) echo "$*" >> "$d/cancelled" ;;
esac
"""
_FAKE_FLUX_JOBS = """#!/bin/bash
[ "$1 $2 $3" = "jobs -n -a" ] || { echo "fake flux: unexpected arguments $*" >&2; exit 2; }
id="${@: -1}"
case "$id" in
  fOKDONE) echo "INACTIVE COMPLETED 0 4515.5" ;;
  fFAILED) echo "INACTIVE FAILED 1 12.0" ;;
  fRUNNING) echo "RUN - - 33.0" ;;
  *) echo "flux-jobs: error: unknown job $id" >&2; exit 1 ;;
esac
"""
_RUN_OUT_FAIL = ("flux-job: task(s) exited with exit code 2\nFri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\n"
                 "flux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n")
_RUN_OUT_OK = "Fri Oct 30 23:30:01 PDT 2026: f35QqToh9 done\nflux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n"


def _batch_case(td, name, out_text, hosts_text, flux_line):
    """Render the detached submit script, run it under bash with a fake flux (it records the id), then evaluate with finish_batch against the fake flux's
    state line `flux_line`. Returns (finish result, rc file text, log text, script text, id file text, record, fake flux path, render)."""
    base = Path(td) / name
    sd, bindir = base / "stage", base / "bin"
    sd.mkdir(parents=True)
    bindir.mkdir()
    for n, body in (("flux", _FAKE_FLUX), ("dbcast", "#!/bin/sh\nexit 0\n")):
        (bindir / n).write_text(body)
        (bindir / n).chmod(0o755)
    (bindir / "state").write_text(flux_line + "\n")
    out = base / "run.out"
    if out_text is not None:
        out.write_text(out_text)
    if hosts_text is not None:
        (sd / "hosts.txt").write_text(hosts_text)
    run_sh = f"#!/bin/bash\n#flux: --nodes 2\n#flux: --output {out}\nflux bulksubmit --nodes 1 {sd}/submit_moirai.sh '{{}}' ::: /r/c.csv\n"
    r = render_batch("/o.sh", "/e", str(sd), run_sh=run_sh, run_script=str(base / "run.sh"), modules=())
    script = base / "batch.sh"
    script.write_text(r["batch_wrapper"])
    p = subprocess.run(["bash", str(script)], env={"PATH": f"{bindir}:/usr/bin:/bin"}, capture_output=True, text=True, timeout=60)
    rec = {"id": "x-1", "stage": "moirai_smoke", "pid": 0, "started": time.time(), "rc_file": str(base / "x.rc"), "log": str(base / "x.log"), "script": str(base / "x.sh"),
           "submit_rc_file": str(base / "x.submit.rc"), "idfile": r["flux_job_id_file"], "flux_job_id": "", "batch_output": r["batch_output"], "hosts_file": r["hosts_file"]}
    (base / "x.json").write_text(json.dumps(rec))
    (base / "x.log").write_text(p.stdout + p.stderr)
    fb = finish_batch(rec, str(bindir / "flux"), 1, 0)
    return fb, _read(base / "x.rc"), (base / "x.log").read_text(), r["batch_wrapper"], _read(r["flux_job_id_file"]), rec, str(bindir / "flux"), r, p.returncode


def _attach_selftest(td) -> dict:
    """moirai_run_attach on a temp pipeline (MOIRAI_AGENTS_PIPELINES_DIR pointed at the temp dir for the duration) with a fake flux: a COMPLETED rc 0 job passes
    'ran' and 'exit_0' of moirai_validate, a FAILED one fails exit_0, an unfinished or unknown job and a bad stage are refused with nothing recorded,
    result_json ok=false gives a non-zero rc with its problems in the log."""
    cases = {}
    td = Path(td)
    bindir = td / "attbin"
    bindir.mkdir()
    fake = bindir / "flux"
    fake.write_text(_FAKE_FLUX_JOBS)
    fake.chmod(0o755)
    (td / "ok.json").write_text(json.dumps({"ok": True, "problems": []}))
    (td / "bad.json").write_text(json.dumps({"ok": False, "problems": ["af3_msa output missing"]}))
    (td / "batch.out").write_text("step done\n")
    saved = os.environ.get("MOIRAI_AGENTS_PIPELINES_DIR")
    os.environ["MOIRAI_AGENTS_PIPELINES_DIR"] = str(td / "pipelines")
    try:
        pid = P.new("run", "attachtest", source="/x", command="x")["id"]

        def ready(cc):
            order = P.KINDS["run"]
            for s in order[:order.index("moirai_smoke")]:
                cc["stages"][s]["status"] = "passed"
        P.update(pid, ready)
        raw = P.work(P.load(pid)) / "traces" / "moirai_smoke" / "raw"
        raw.mkdir(parents=True)
        (raw / "prov_nodeA.pfw.gz").write_bytes(b"")

        def att(stage="moirai_smoke", **kw):
            try:
                return attach_job(pid, stage, flux=str(fake), pause=0, **kw)
            except P.PipelineError as e:
                return {"status": "error", "error": str(e)}

        def njobs():
            return len(list((P.pdir(pid) / "jobs").glob("*.json"))) if (P.pdir(pid) / "jobs").is_dir() else 0

        def bad_checks():
            r = _validate(pid, "moirai_smoke")
            return r, {x["name"] for x in r["failed"]}

        a = att(flux_job_id="fOKDONE", output_file=str(td / "batch.out"), result_json=str(td / "ok.json"))
        cases["attach_completed_rc0"] = a.get("status") == "ok" and a.get("rc") == 0 and a.get("source") == "attached" and a.get("trace_files") == 1
        j = J.get(pid, a["job"], 2000) if a.get("job") else {}
        cases["attach_job_record"] = j.get("status") == "done" and j.get("rc") == 0 and j.get("source") == "attached" and "step done" in j.get("log_tail", "") and j.get("elapsed") == 4515.5
        r, bad = bad_checks()
        cases["attach_validate_ran_exit0"] = "ran" not in bad and "exit_0" not in bad and r["total"] >= 3
        cases["attach_hosts_from_inventory"] = "host_service_trace" not in bad and P.stage(P.load(pid), "moirai_smoke")["outputs"].get("per_host_traces") == {"nodeA": 1}
        f = att(flux_job_id="fFAILED")
        cases["attach_failed_rc_nonzero"] = f.get("status") == "ok" and f.get("rc") not in (0, None)
        r, bad = bad_checks()
        cases["attach_failed_exit0_fails"] = "exit_0" in bad and "ran" not in bad
        before = njobs()
        u = att(flux_job_id="fRUNNING")
        cases["attach_unfinished_refused"] = u.get("status") == "error" and "not finished" in u.get("error", "")
        n = att(flux_job_id="fNOSUCH")
        cases["attach_unknown_refused"] = n.get("status") == "error" and "unknown" in n.get("error", "")
        cases["attach_bad_id_refused"] = att(flux_job_id="f1; rm -rf x").get("status") == "error"
        cases["attach_bad_stage_refused"] = att(stage="query", flux_job_id="fOKDONE").get("status") == "error"
        cases["attach_unreadable_result_json_refused"] = att(flux_job_id="fOKDONE", result_json=str(td / "missing.json")).get("status") == "error"
        cases["attach_unreadable_output_refused"] = att(flux_job_id="fOKDONE", output_file=str(td / "missing.out")).get("status") == "error"
        cases["attach_refusals_record_nothing"] = njobs() == before
        s = att(flux_job_id="fOKDONE", result_json=str(td / "bad.json"))
        j2 = J.get(pid, s["job"], 2000) if s.get("job") else {}
        cases["attach_study_false_rc_nonzero"] = s.get("status") == "ok" and s.get("rc") != 0 and s.get("result_json_ok") is False and "af3_msa output missing" in j2.get("log_tail", "")
        cases["attach_rc_not_a_parameter"] = "rc" not in inspect.signature(attach_job).parameters
    finally:
        if saved is None:
            os.environ.pop("MOIRAI_AGENTS_PIPELINES_DIR", None)
        else:
            os.environ["MOIRAI_AGENTS_PIPELINES_DIR"] = saved
    th = trace_hosts(["/r/prov_nodeA.pfw.gz", "/r/prov_nodeB-spill.pfw.gz", "/r/other.pfw.gz"])
    cases["attach_trace_hosts"] = sorted(th) == ["nodeA", "nodeB"] and derive_rc({"result": "COMPLETED", "returncode": "0"}) == 0 and derive_rc({"result": "CANCELED", "returncode": "-"}) == 1 and derive_rc({"result": "FAILED", "returncode": "3"}) == 3
    return cases


def _selftest() -> dict:
    """Wrapper write, idempotence and mismatch handling on a temp dir, through the pure render and sync_wrapper; the detached batch submit script run with a fake
    flux and the state/evaluation read from flux (finish_batch); the resolver on a fake venv; moirai_run_attach on a temp pipeline with a fake flux."""
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

        # D2: resolver on a fake venv; python entry script is not mistaken for the binary; defaulted venv renders the same text as an explicit one
        venv = Path(td) / "moirai-venv"
        svc = venv / "lib" / "python3.11" / "site-packages" / "dftracer" / "bin" / "dftracer_service"
        svc.parent.mkdir(parents=True)
        svc.write_text("#!/bin/sh\nexit 0\n")
        svc.chmod(0o755)
        (venv / "bin").mkdir()
        entry = venv / "bin" / "moirai-service"
        entry.write_text(f"#!{venv}/bin/python\nfrom moirai.service import main\n")
        entry.chmod(0o755)
        rs = resolve_dftracer_service(str(entry), str(venv))
        cases["resolver_venv"] = rs["path"] == os.path.realpath(svc) and rs["binary_kind"] == "python-entry-script" and not rs["embedded_relative_path"]
        cases["resolver_no_venv_python_script"] = resolve_dftracer_service(str(entry), "")["path"] == ""
        svc.chmod(0o644)
        try:
            check_service_venv(str(venv))
            cases["venv_not_executable_refused"] = False
        except P.PipelineError as e:
            cases["venv_not_executable_refused"] = "service_venv" in str(e) and "dftracer_service" in str(e)
        svc.chmod(0o755)
        cases["venv_ok"] = check_service_venv(str(venv))["path"] == os.path.realpath(svc)
        cases["default_venv_flat"] = default_service_venv({"moirai.python": f"{venv}/bin/python"}) == str(venv)
        cases["default_venv_nested"] = default_service_venv({"moirai": {"python": f"{venv}/bin/python"}}) == str(venv) and default_service_venv({}) == ""
        real = os.path.realpath(svc)
        args = ("/o.sh", "/e.sh", "/t/s", "/data", "/moirai-agents-x")
        explicit = render_submit_wrapper(*args, venv=str(venv), dft_service=real)
        defaulted = render_submit_wrapper(*args, venv=default_service_venv({"moirai.python": f"{venv}/bin/python"}), dft_service=real)
        cases["default_venv_identical"] = explicit == defaulted
        cases["python_fallback"] = "import dftracer,os;print(os.path.dirname(dftracer.__file__))" in explicit and "../../dftracer/bin" not in explicit
        cases["explicit_env_first"] = explicit.index('if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]') < explicit.index("import dftracer")
        cases["no_warning_with_venv"] = render_batch(*args, venv=str(venv), dft_service=real)["warnings"] == []

        # D1 (chunk job failed although the batch returned 0), now evaluated from flux + run.out by finish_batch: fake flux says INACTIVE COMPLETED 0
        done = "INACTIVE COMPLETED 0 12.0"
        fb, rcf, log, script, idf, rec, fx, r, srt = _batch_case(td, "fail", _RUN_OUT_FAIL, "nodeA\n", done)
        cases["batch_fail_detected"] = rcf == "4" and fb["state"] == "failed" and "f35QqToh9" in log and "FAILED" in log
        fb, rcf, log, *_ = _batch_case(td, "clean", _RUN_OUT_OK, "nodeA\n", done)
        cases["batch_clean_pass"] = rcf == "0" and fb["state"] == "completed" and "clean" in log
        fb, rcf, log, *_ = _batch_case(td, "nohosts", _RUN_OUT_OK, None, done)
        cases["batch_hosts_missing"] = rcf == "5" and "no per-chunk wrapper ran" in log
        fb, rcf, *_ = _batch_case(td, "emptyhosts", _RUN_OUT_OK, "", done)
        cases["batch_hosts_empty"] = rcf == "5"
        fb, rcf, *_ = _batch_case(td, "noout", None, "nodeA\n", done)
        cases["batch_output_unreadable"] = rcf == "6"
        cases["batch_output_parsed"] = r["batch_output"].endswith("/fail/run.out") and batch_output_path("#!/bin/bash\n#flux: --output=/x/y.out\n") == "/x/y.out"
        fb, rcf, *_ = _batch_case(td, "flfail", _RUN_OUT_OK, "nodeA\n", "INACTIVE FAILED 3 1.0")
        cases["batch_flux_failure_rc"] = rcf == "3" and fb["state"] == "failed"
        fb, rcf, *_ = _batch_case(td, "cancel", _RUN_OUT_OK, "nodeA\n", "INACTIVE CANCELED - 1.0")
        cases["batch_cancelled_state"] = rcf == "1" and fb["state"] == "cancelled"

        # f3j9BPvr1Agw: the submit never cancels, records the id, and the state comes from flux (queued / running / ended after a "restart" / unknown id)
        cases["batch_no_cancel_trap"] = "trap" not in script and "flux cancel" not in script and "flux watch" not in script and "never cancels the flux job" in script
        cases["batch_id_recorded"] = idf == "fFAKEBATCH" and srt == 0
        fb, rcf, *_ = _batch_case(td, "queued", _RUN_OUT_OK, "nodeA\n", "SCHED - - 0.0")
        cases["status_queued_from_flux"] = fb["state"] == "queued" and fb["flux_job_id"] == "fFAKEBATCH" and rcf == "" and fb["rc"] is None
        fb, rcf, log, script, idf, rec, fx, r, srt = _batch_case(td, "restart", _RUN_OUT_OK, "nodeA\n", "RUN - - 5.0")
        running = fb["state"] == "running" and rcf == ""
        (Path(fx).parent / "state").write_text("INACTIVE COMPLETED 0 99.0\n")
        rec2 = json.loads((Path(rec["script"]).with_suffix(".json")).read_text())     # a fresh process reads the record only
        fb2 = finish_batch(rec2, fx, 1, 0)
        cases["finish_after_server_restart"] = running and rec2["flux_job_id"] == "fFAKEBATCH" and fb2["state"] == "completed" and _read(rec["rc_file"]) == "0" and fb2["flux_runtime"] == "99.0"
        rec3 = dict(rec2, flux_job_id="fGONE")
        Path(rec3["rc_file"]).unlink()
        cases["status_lost_only_when_flux_unknown"] = finish_batch(rec3, fx, 1, 0)["state"] == "lost" and running and fb2["state"] != "lost"
        cancelled_log = Path(fx).parent / "cancelled"
        cases["cancel_only_by_stop"] = (not cancelled_log.exists())
        st = stop_batch(dict(rec2, flux_job_id="fFAKEBATCH"), fx)
        (Path(fx).parent / "state").write_text("RUN - - 5.0\n")
        st = stop_batch(dict(rec2, flux_job_id="fFAKEBATCH"), fx)
        cases["cancel_only_by_stop"] = cases["cancel_only_by_stop"] and st["stopped"] is True and "cancel fFAKEBATCH" in _read(cancelled_log)
        av = detached_argv("/j/a.sh", "/j/a.submit.rc", {"PATH": "/usr/bin:/bin", "HOME": "/h", "USER": "u", "TERM": "xterm"})
        cases["detached_argv"] = av[:3] == ["nohup", "env", "-i"] and "HOME=/h" in av and "USER=u" in av and "TERM=xterm" in av and "PATH=/usr/bin:/bin" not in av and av[-2] == "-lc" and "bash /j/a.sh" in av[-1]

        # attach: a finished, externally submitted flux batch (fake flux, temp pipeline)
        cases.update(_attach_selftest(td))
    failed = [k for k, v in cases.items() if not v]
    line = "moirai wrapper selftest PASS " + " ".join(f"{k}=ok" for k in cases) if not failed else "moirai wrapper selftest FAIL " + ",".join(failed)
    return {"status": "ok" if not failed else "error", "line": line, "cases": cases}


def register(mcp):
    @mcp.tool()
    def moirai_wrapper_write(pipeline: str, stage: str, service_venv: str = "", data_dir: str = "", original_script: str = "") -> dict:
        """Write ONLY the per-chunk submit wrapper <work>/traces/<stage>/submit_moirai.sh (mode 755) and create the stage and raw dirs. Use right after
        integrate, BEFORE elmerfold_run_script: the run.sh generator requires --submit-script to be an existing file. The text is the one moirai_batch_plan shows.
        It submits and starts nothing. Idempotent: the same text changes nothing (action unchanged); a different file on disk is rewritten (action rewritten).
        pipeline: pipeline id. stage: moirai_smoke|baseline|live_run. service_venv: venv holding dftracer/bin/dftracer_service; default the venv root of the integrate
        stage's moirai.python (the text is identical whether passed or defaulted); an error naming the fix when <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service
        is missing or not executable. data_dir: MOIRAI_DATA_DIR (default the annotated tree). original_script: the app's own submit script the wrapper runs
        (default <annotated>/monomer-structure-prediction/submit_smsp.sh).
        Returns {status, path, sha256, action: created|unchanged|rewritten|mode_fixed, previous_sha256, raw_dir, original_script, service_venv, warnings} or {status: error, error}."""
        if stage not in STAGES:
            return {"status": "error", "error": f"stage must be one of {STAGES}"}
        try:
            _, r = _plan(pipeline, stage, "", "pdebug", 2400, data_dir, service_venv, original_script)
            w = sync_wrapper(r["stage_dir"], r["wrapper_script"])
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}
        return {"status": "ok", "path": w["path"], "sha256": w["sha256"], "action": w["action"], "previous_sha256": w["previous_sha256"], "raw_dir": w["raw_dir"],
                "original_script": r["original_script"], "service_venv": r["service_venv"], "warnings": r["warnings"]}

    @mcp.tool()
    def moirai_wrapper_selftest() -> dict:
        """Self-test of the wrapper tools on a temp dir (no pipeline, nothing on the cluster): create, sha256, mode 755, dirs, idempotence, detection and rewrite
        of a differing file, a changed render, mode repair, a run.sh not referencing the wrapper refused; the resolver on a fake venv (python entry script),
        defaulted vs explicit venv rendering the same text, the python-based fallback; the DETACHED batch submit script run under bash with a fake flux (records
        the flux job id, contains no cancel trap), and the state / post-run evaluation read from flux and run.out by finish_batch: a failing run.out (rc 4, chunk
        job id named), a clean one (rc 0), a missing/empty hosts.txt (rc 5), an unreadable output (rc 6), a failed or cancelled flux result, queued and running
        states, evaluation after a simulated server restart, 'lost' only for an id flux does not know, cancel only through stop_batch; and moirai_run_attach on a
        temp pipeline with a fake flux (attach_* cases). Use after any change to the wrapper text, the batch submit, the status/finish logic, the resolver or the
        attach tool. Returns {status, line, cases}: line is 'moirai wrapper selftest PASS ...' or 'moirai wrapper selftest FAIL <case>'."""
        return _selftest()

    @mcp.tool()
    def moirai_smoke_run(pipeline: str, command: str = "", launcher: str = "", wait: int = 600, limit: int = 1800, batch_script: str = "", queue: str = "pdebug",
                         data_dir: str = "", service_venv: str = "", original_script: str = "") -> dict:
        """Run the annotated app on the small test under moirai-service. Use after integrate. It stops the service however the app ends; limit: seconds before it is
        stopped. The Dashboard tab follows it live. batch_script: path of the generated flux-batch run.sh (Elmerfold), generated WITH --submit-script <wrapper path>
        from moirai_wrapper_write; run.sh is not modified: the wrapper <work>/traces/<stage>/submit_moirai.sh is compared with the freshly rendered text
        (rewritten, mode 755, and reported when it differs) and a DETACHED submit (own session, nohup, clean env, no cancel on any signal) runs
        `flux batch -t <limit/60> --queue <queue> <run.sh>` and records the flux job id; the batch then belongs to Flux, not to this server: a server restart does not
        cancel it, and only moirai_run_stop does. Its state comes from flux (moirai_run_status), and when it ends moirai_run_status / moirai_validate read the run.sh
        `#flux: --output` file: rc 4 naming the chunk job, 5 no hosts.txt, 6 output unreadable when a chunk job failed even though the batch returned 0.
        A run.sh that does not reference the wrapper is refused before anything is written. Run moirai_batch_plan first and get the user's yes.
        wait: seconds to wait for the flux job id to be recorded (the job itself is not waited for in batch mode). queue: the batch queue. data_dir: MOIRAI_DATA_DIR
        (default the annotated tree). service_venv: venv holding dftracer/bin/dftracer_service (default the venv root of integrate's moirai.python; verified executable).
        original_script: the app's own submit script the wrapper runs (default <annotated>/monomer-structure-prediction/submit_smsp.sh). A flux batch already submitted
        by hand and FINISHED is not run here: moirai_run_attach. Returns {status, job, rc, elapsed, traces, flux_job_id, flux_state, wrapper: {action, sha256,
        previous_sha256}, log_tail}."""
        if batch_script:
            return _start_batch(pipeline, "moirai_smoke", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        return _start(pipeline, "moirai_smoke", command, launcher, 1, "", wait, limit)

    @mcp.tool()
    def moirai_baseline_run(pipeline: str, command: str = "", launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = 3600,
                            batch_script: str = "", queue: str = "pdebug", service_venv: str = "", original_script: str = "") -> dict:
        """Start the real baseline run under moirai-service, in the background, on what the user approved at the gate. command: the full run. launcher: e.g. 'flux run -N4 -n32'.
        nodes: one service starts per node. data_dir: where its I/O is traced. limit: seconds before it is stopped. batch_script, queue, service_venv, original_script: as in
        moirai_smoke_run (batch mode ignores command, launcher, nodes: the run.sh decides; service_venv defaults from integrate's moirai.python; the wrapper on disk is
        verified against the fresh render and rewritten, with a report, when it differs; the flux batch is submitted DETACHED and its flux job id recorded, so a server
        restart never cancels it (only moirai_run_stop does); state and the chunk-failure evaluation come from flux and run.out in moirai_run_status / moirai_validate).
        The Dashboard tab shows events as they appear. A finished flux batch submitted by hand is attached with moirai_run_attach instead.
        Returns {status, job, traces, flux_job_id, flux_state, wrapper}: watch with moirai_run_status."""
        if batch_script:
            return _start_batch(pipeline, "baseline", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        if not command:
            raise P.PipelineError("give command (with launcher) or batch_script")
        return _start(pipeline, "baseline", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_live_run(pipeline: str, command: str = "", launcher: str = "", nodes: int = 1, data_dir: str = "", wait: int = 30, limit: int = LIVE_LIMIT_S,
                        batch_script: str = "", queue: str = "pbatch", service_venv: str = "", original_script: str = "") -> dict:
        """Start a LIVE run: the long (multi-hour) run of an app a run pipeline already annotated, under moirai-service, in the background, with provenance written live
        (the Live Run view draws the graph as it forms). pipeline: a live pipeline (pipeline_new(kind='live', parent=<run pipeline>)); it uses the parent's annotated
        tree and moirai_env.sh. command: the real run (default the live pipeline's command, which defaults to the parent's). limit: seconds before it is stopped
        (default 12 h). batch_script, queue, service_venv, original_script: as in moirai_baseline_run. In batch mode the flux batch is submitted DETACHED from the
        server (a web UI / tool-server restart, which happens regularly, does NOT cancel it, queued or running); the flux job id is recorded in the job record and the
        stage outputs, moirai_run_status reads the real state (queued/running/completed/failed/cancelled/timeout/lost) from flux, and only moirai_run_stop cancels.
        Returns {status, job, traces, flux_job_id, flux_state}: follow with moirai_run_status(pipeline,
        'live_run'); when it ends, moirai_validate(pipeline, 'live_run'), then the publish_data stage."""
        if batch_script:
            return _start_batch(pipeline, "live_run", batch_script, queue, limit, data_dir, service_venv, wait, original_script)
        return _start(pipeline, "live_run", command, launcher, nodes, data_dir, wait, limit)

    @mcp.tool()
    def moirai_run_attach(pipeline: str, stage: str, flux_job_id: str, output_file: str = "", result_json: str = "") -> dict:
        """Attach a flux batch that was submitted BY HAND (e.g. BioTox: biotox_run_script + biotox_run_check, then `flux batch launch.sh`) and has already FINISHED,
        so that moirai_validate can judge the stage like a started run. Prefer moirai_smoke_run / moirai_baseline_run when they can submit the run; attach only a
        finished externally submitted job. It submits and cancels nothing and writes only the pipeline's own jobs dir (job record, log, rc file) and the stage outputs
        (job, traces <work>/traces/<stage>/raw, mode attached, flux_job_id, hosts_file cleared). The job gets source 'attached'.
        The rc is never taken from the caller: it is derived from `flux jobs -n -a -o '{state} {result} {returncode} {runtime}' <id>` (retried; the job must be
        INACTIVE): 0 only when result is COMPLETED and returncode 0, else flux's non-zero returncode or 1; with result_json (check_study.py output of the run dir)
        its boolean 'ok' must also be true for rc 0 (false: rc 1, the problems are at the end of the job log).
        pipeline: pipeline id. stage: moirai_smoke|baseline (other stages are refused). flux_job_id: the batch job id, e.g. f3iKMXkX8ykB. output_file: the batch --output
        file; its last 20000 bytes are copied into the job log. result_json: path of check_study's result.json; refused when unreadable or without a boolean 'ok'.
        Refused (status error, nothing written) when the flux job is unknown or not finished, a job of the stage is still running, the stage is not ready, or a file is unreadable.
        Returns {status, job, source: attached, flux_job_id, flux_state, flux_result, flux_returncode, rc, elapsed, traces, trace_files, result_json_ok, problems, warnings, next}
        or {status: error, error}. Then run moirai_validate(pipeline, stage): without a wrapper hosts.txt the per-host checks use only the prov_<host> trace file names."""
        try:
            return attach_job(pipeline, stage, flux_job_id, output_file, result_json)
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def moirai_batch_plan(pipeline: str, stage: str, batch_script: str = "", queue: str = "pdebug", limit: int = 2400, data_dir: str = "", service_venv: str = "",
                          original_script: str = "") -> dict:
        """DRY RUN of the batch mode: nothing is written, submitted or started. Returns the text of the per-chunk submit wrapper, its path and whether the file on disk
        is missing, in sync or differs (wrapper_on_disk). The wrapper itself is written by moirai_wrapper_write, which must run BEFORE elmerfold_run_script (the
        generator needs an existing --submit-script). Order: moirai_wrapper_write, elmerfold_run_script with submit_script (and cwd), elmerfold_run_check with
        submit_script, this tool with batch_script, the user's yes, moirai_smoke_run. Once batch_script (the generated run.sh) is given it checks run.sh (one bulksubmit
        line, its ':::' input list, the wrapper path and the annotated tree in it) and prints the exact submit command, the detached batch submit script text (batch_wrapper:
        submits, records the flux job id, never cancels; the run.sh `#flux: --output` file, batch_output, is read after the batch by moirai_run_status/moirai_validate)
        and the service venv used.
        stage: moirai_smoke|baseline|live_run. limit: seconds (the batch gets ceil(limit/60) minutes). service_venv: default the venv root of integrate's moirai.python.
        original_script: default the annotated copy's submit_smsp.sh.
        Returns {status, wrapper_path, wrapper_script, wrapper_on_disk, batch_wrapper, batch_output, flux_job_id_file, run_sh_check, submit_command, resolver, service_venv, warnings} or {status: error, error}."""
        try:
            _, r = _plan(pipeline, stage, batch_script, queue, limit, data_dir, service_venv, original_script)
        except (P.PipelineError, OSError) as e:
            return {"status": "error", "error": str(e)}
        r["wrapper_on_disk"] = wrapper_state(r["stage_dir"], r["wrapper_script"])
        if not batch_script:
            r["submit_command"] = r["submit_command"].replace(r["run_script_path"], "<run.sh>")
            r["run_script_path"] = ""
            r["note"] = (f"run.sh not given: write the wrapper with moirai_wrapper_write (on disk: {r['wrapper_on_disk']['state']}), generate run.sh with "
                         f"elmerfold_run_script --submit-script {r['wrapper_path']} (the generator requires it to exist), then plan again with batch_script")
        elif r["wrapper_on_disk"]["state"] != "in_sync":
            r["warnings"].append(f"wrapper on disk is {r['wrapper_on_disk']['state']}: run moirai_wrapper_write (the run would rewrite it and say so)")
        r["warnings"] += ["UNCONFIRMED: whether `moirai-service start` returns by itself (the wrapper backgrounds it and waits for the ring either way)",
                          "UNCONFIRMED: submit_smsp.sh puts ${ELMER_ENV}/lib first in LD_LIBRARY_PATH for the app (libstdc++ shadowing of moirai/dftracer libs)",
                          "UNCONFIRMED: trace file names per host; whether the GPU server runs on the same node as the driver",
                          "UNCONFIRMED: the detached submit runs in `env -i HOME USER TERM bash -lc`: flux and `module` must come from the login profile (not run on a real cluster)"]
        r.setdefault("note", "dry run: nothing started or written; the real run verifies the wrapper (mode 755, rewritten when it differs), then submits the batch DETACHED "
                             "(a server restart does not cancel it), which runs the submit command and records the flux job id")
        return r

    @mcp.tool()
    def moirai_batch_render(original: str, env: str, stage_dir: str, data_dir: str = "/data", shm_base: str = "/moirai-agents-test", queue: str = "pdebug", limit: int = 2400,
                            run_sh: str = "", run_script: str = "", annotated: str = "", service_venv: str = "") -> dict:
        """Pure text render, no pipeline or files needed (sandbox checks, experiments). original: path of the app's own submit script; env: moirai_env.sh path;
        stage_dir: <work>/traces/<stage>; shm_base: ring name prefix (the job id is appended at run time). Optional run_sh: text of a generated run.sh to check against
        the wrapper (status error when it does not run the wrapper, has no ':::' or does not hold `annotated`); its `#flux: --output <path>` header is the file the
        post-run evaluation reads after the batch (batch_output). batch_wrapper is the detached submit script: it submits, writes the flux job id to flux_job_id_file
        and never cancels. Returns {status, wrapper_path, wrapper_script, batch_wrapper, batch_output, flux_job_id_file, submit_command, run_sh_check, warnings, error?}."""
        return render_batch(original, env, stage_dir, data_dir, shm_base, queue, limit, run_sh, run_script, annotated, service_venv)

    @mcp.tool()
    def moirai_service_resolve(binary: str = "", venv: str = "") -> dict:
        """Find the real dftracer_service. The installed package is the truth: <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service (tried first). The venv's
        `moirai-service` is a Python entry-point script (binary_kind python-entry-script), which says nothing about it; only a compiled moirai-service holding
        ../../dftracer/bin/dftracer_service is also tried relative to its real path. binary: path of moirai-service (default: on PATH).
        venv: venv root (e.g. the root of integrate's moirai.python). Returns {binary, binary_kind: python-entry-script|binary|'', embedded_relative_path, tried, path, exists};
        export MOIRAI_DFTRACER_SERVICE=<path> when the default is missing."""
        r = resolve_dftracer_service(binary, venv)
        r["exists"] = bool(r["path"])
        return r

    @mcp.tool()
    def moirai_run_status(pipeline: str, stage: str) -> dict:
        """Follow a run: job state, log tail and what the trace holds so far (readable while the run is going). Use while it runs, to report progress or spot a stall,
        and after the web server or tool server restarted. For a flux batch the state is READ FROM FLUX (`flux jobs` of the recorded flux_job_id), never from a
        server process: flux_state is queued (waiting in the Flux queue), running, completed, failed, cancelled, timeout, submitting (id not recorded yet), lost (flux
        does not know the id, or the submit vanished without one) or unreachable (flux cannot be run from here). When flux says the job ended, this call performs the
        post-run evaluation once (rc 0 only for COMPLETED 0 with no failed chunk job in run.out and a non-empty hosts.txt; rc 4/5/6 otherwise, see SKILL) and writes the
        rc into the job; the stage outputs get flux_job_id and flux_state. status is running (also while queued), done, lost or unknown.
        Returns {job, status, rc, elapsed, timed_out, flux_job_id, flux_state, flux_detail, counts, log_tail}."""
        o = P.stage(P.load(pipeline), stage)["outputs"]
        if not o.get("job"):
            raise P.PipelineError(f"stage {stage} has no job: start it with moirai_smoke_run / moirai_baseline_run / moirai_live_run, or attach a finished one")
        j = batch_job(pipeline, o["job"], 2000)
        return {"job": j["id"], "status": j["status"], "rc": j["rc"], "elapsed": j["elapsed"], "timed_out": j.get("timed_out", False),
                "flux_job_id": j.get("flux_job_id", ""), "flux_state": j.get("flux_state", ""), "flux_detail": j.get("flux_detail", {}),
                "counts": T.read(o["traces"]).counts(), "log_tail": j["log_tail"]}

    @mcp.tool()
    def moirai_run_stop(pipeline: str, stage: str) -> dict:
        """Stop a run on the user's request, or when it is clearly stuck. This is the ONLY thing that cancels a flux batch: for a detached batch it runs
        `flux cancel <recorded flux job id>` (when flux says it has not ended; a queued job is cancelled too); a restart of the server never does. The app jobs then get
        TERM and stop their service; moirai_run_status shows the job as cancelled. For a plain command the job's process group is terminated. An attached (finished) job
        has nothing to stop. Returns {status, id, stopped, flux_job_id?, note?}."""
        jid = P.stage(P.load(pipeline), stage)["outputs"]["job"]
        rec = _load_rec(pipeline, jid)
        if rec.get("source") == BATCH_SOURCE:
            return stop_batch(rec)
        return J.stop(pipeline, jid)

    @mcp.tool()
    def moirai_validate(pipeline: str, stage: str) -> dict:
        """Validate a moirai run: exit 0, a trace from the service and not spill files, activities and entities recorded, no dangling edge, every entity type in
        the spec seen; in batch mode also one trace per host that ran the wrapper (hosts.txt) and no host with only spill traces. A detached flux batch that has
        ended is evaluated here too (state from flux, chunk failures from run.out) when no earlier call did, so it works after a server restart; queued/running
        flux jobs give an inconclusive 'finished'. An attached run (moirai_run_attach)
        is judged the same way; without a wrapper hosts.txt the per-host evidence comes only from prov_<host> trace file names (spill-only hosts fail
        host_service_trace; no such names: no per-host judgement). exit_0 is never relaxed. stage: moirai_smoke|baseline|live_run.
        Use after the job ended. Returns the verdict {status, failed, unknown, passed, total}."""
        return _validate(pipeline, stage)
