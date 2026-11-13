"""Last stage of the run pipeline: final_provenance/, a folder that reproduces the run for someone who has neither this session nor this account, and the proof that it does.

Same shape as dftracer-agents' reproduction packages: config.ini is the only file with site values, every script sources lib_load_config.sh, the instrumentation is a patch
against a pinned app, and a standalone checker judges the result. It is verified by running it from a fresh copy in a clean login shell.

Two modes. generic: the app runs in the shell that runs 04_run.sh, under a moirai-service started there. batch: the app is launched by its own generator as a `flux batch` whose chunk jobs run on
compute nodes; each chunk job runs a wrapper that starts its own moirai-service, so 04_run.sh never starts a service on the login node and waits for and judges the batch job.
"""
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import moirai_env as M
from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGE = "package"
LOADER = "lib_load_config.sh"
JOBID = re.compile(r"\bf[1-9A-Za-z]{9,}\b")
PIN = r'''
import importlib.metadata as m, json
out = {}
for n in ("dftracer", "moirai", "dftracer-utils"):
    try:
        d = m.distribution(n)
    except m.PackageNotFoundError:
        continue
    u = d.read_text("direct_url.json")
    u = json.loads(u) if u else {}
    v = u.get("vcs_info") or {}
    out[n] = f"{n} @ git+{u['url']}@{v['commit_id']}" if v.get("commit_id") else f"{n}=={d.version}"
print(json.dumps(out))
'''
LIB = r'''#!/bin/bash
# Sourced by every script: reads ../config.ini, sets the derived paths. No script here holds a user, workspace or job path.
set -euo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CONFIG:-$PKG_DIR/config.ini}"
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*(#|\[|$) ]] && continue
  key="${line%%=*}"; val="${line#*=}"
  if [[ -z "${!key:-}" ]]; then eval "export $key=\"\$(printf %s \"$(printf %s "$val" | sed 's/"/\\"/g')\")\""; else export "$key"; fi
done < "$CONFIG"
require() { for v in "$@"; do [[ -n "${!v:-}" ]] || { echo "config: $v is required" >&2; exit 2; }; done; }
log() { echo "[$(date +%H:%M:%S)] $(basename "$0"): $*"; }
require OUTPUT_ROOT VENV SCALE
export SRC="$OUTPUT_ROOT/src" TRACES="$OUTPUT_ROOT/traces/$SCALE/raw" PY="$VENV/bin/python"
mkdir -p "$OUTPUT_ROOT" "$TRACES"
'''
STEPS = {
    "00_check_prereqs.sh": r'''# Commands, and that the runtime this package needs can be reached.
for c in git patch python3; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 1; }; done
[[ "$REUSE_VENV" == 1 ]] && { "$PY" -c "import moirai" || { echo "REUSE_VENV=1 but $PY has no moirai" >&2; exit 1; }; }
log "ok"
''',
    "01_get_source.sh": r'''# The application at its pinned state, plus the provenance patch.
rm -rf "$SRC"
if [[ -n "$APP_URL" ]]; then
  git clone -q "$APP_URL" "$SRC" && git -C "$SRC" checkout -q "$APP_REF"
else
  mkdir -p "$SRC" && tar -xzf "$PKG_DIR/inputs/source.tar.gz" -C "$SRC"
fi
(cd "$SRC" && patch -s -p1 < "$PKG_DIR/patches/provenance.patch")
log "source ready in $SRC"
''',
    "02_setup_env.sh": r'''# The runtime, at the versions the run used. REUSE_VENV=1 uses an existing venv.
if [[ "$REUSE_VENV" == 1 ]]; then log "reusing $VENV"; exit 0; fi
python3 -m venv "$VENV"
"$PY" -m pip install -q --upgrade pip scikit-build-core pybind11
DFTRACER_WHEEL=1 "$PY" -m pip install -q "$DFTRACER_SPEC"
"$PY" -m pip install -q --no-build-isolation "$MOIRAI_SPEC"
[[ -z "$UTILS_SPEC" ]] || "$PY" -m pip install -q "$UTILS_SPEC"
''',
    "03_build.sh": r'''# Build the patched application against moirai.
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
LIBS="$SITE/moirai/lib64:$SITE/moirai/lib:$SITE/dftracer/lib64:$SITE/dftracer/lib"
export MOIRAI_CFLAGS="-I$SITE/moirai/include -I$SITE/dftracer/include" MOIRAI_LDFLAGS="-L$SITE/moirai/lib64 -lmoirai -Wl,-rpath,$SITE/moirai/lib64"
export CFLAGS="$MOIRAI_CFLAGS ${CFLAGS:-}" CXXFLAGS="$MOIRAI_CFLAGS ${CXXFLAGS:-}" LDFLAGS="$MOIRAI_LDFLAGS ${LDFLAGS:-}" LD_LIBRARY_PATH="$LIBS:${LD_LIBRARY_PATH:-}"
export PATH="$VENV/bin:$PATH"
cd "$SRC"
if [[ -n "$BUILD_COMMAND" ]]; then log "building"; eval "$BUILD_COMMAND"; else log "nothing to build"; fi
''',
    "04_run.sh": r'''# The run under moirai-service, which starts first and stops last.
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
export LD_LIBRARY_PATH="$SITE/moirai/lib64:$SITE/moirai/lib:$SITE/dftracer/lib64:$SITE/dftracer/lib:${LD_LIBRARY_PATH:-}"
export PATH="$VENV/bin:$PATH" PYTHONPATH="$SRC:${PYTHONPATH:-}"
export MOIRAI_LOG_FILE="$TRACES/prov" MOIRAI_DATA_DIR="${MOIRAI_DATA_DIR:-$SRC}" MOIRAI_SERVICE_SHM="/moirai-repro-$$" MOIRAI_PC_BLOCKS="${MOIRAI_PC_BLOCKS:-16}"
stop() { trap - EXIT; moirai-service stop; }
trap stop EXIT
moirai-service start
for _ in $(seq 100); do [[ -s "/dev/shm$MOIRAI_SERVICE_SHM" ]] && break; sleep 0.1; done
var="COMMAND_$SCALE"
cd "$SRC"
log "running: ${!var}"
eval "$LAUNCHER ${!var}"
''',
    "05_validate.sh": r'''# Judge the result with the standalone checker: it writes run.json next to the traces.
python3 "$PKG_DIR/scripts/prov_check.py" "$TRACES" "$PKG_DIR/inputs/spec.json" "$PKG_DIR/inputs/expected.json" "$SCALE" "$OUTPUT_ROOT/run.json"
''',
}
RUN_ALL = r'''# Reproduce end to end: prereqs, source and patch, environment, build, run, validate.
d="$(dirname "$0")"
for s in 00_check_prereqs 01_get_source 02_setup_env 03_build 04_run 05_validate; do log "== $s"; bash "$d/$s.sh"; done
log "done: $OUTPUT_ROOT/run.json"
'''
CHECK = r'''#!/usr/bin/env python3
"""Standalone provenance check: python stdlib only. It needs nothing from the tools that made this package.
    prov_check.py <trace dir> <spec.json> <expected.json> <scale> <run.json>
Fails on no trace, a dangling id, an undescribed type, an artifact type with no producer, or counts that differ from the original run."""
import glob, gzip, json, os, sys
traces, spec, expected, scale, out = sys.argv[1:6]
spec, expected = json.load(open(spec)), json.load(open(expected)).get(scale)
types, ents, acts, edges = {}, {}, 0, []
files = sorted(glob.glob(os.path.join(traces, "**", "*.pfw.gz"), recursive=True))
for f in files:
    with gzip.open(f, "rt", errors="replace") as fh:
        for line in fh:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            a = r.get("args") or {}
            if r.get("name") == "ET":
                types[a.get("type")] = a.get("description", "")
            elif r.get("name") == "EH" and a.get("id"):
                ents[a["id"]] = a.get("type", "")
            elif r.get("cat") in ("moirai", "PROV") and r.get("ph") in (1, "X"):
                acts += 1
                for kind, ids in (a.get("relations") or {}).items():
                    edges += [(kind, i) for i in (ids if isinstance(ids, list) else [ids])]
made = {ents.get(i) for k, i in edges if k in ("generated", "updated")}
seen = set(ents.values())
fails = []
if not files or not acts:
    fails.append("no provenance in the trace")
dangling = sorted({i for _, i in edges} - set(ents))
if dangling:
    fails.append(f"{len(dangling)} dangling ids")
fails += [f"type {t} has no description" for t in sorted(seen) if not types.get(t)]
fails += [f"artifact type {t} was not produced" for t in spec.get("scientific_artifacts", []) if t not in made]
got = {"activities": acts, "entities": len(ents), "edges": len(edges)}
if expected:
    fails += [f"{k}: {got[k]}, the original run had {expected[k]}" for k in got if got[k] != expected.get(k)]
json.dump({"scale": scale, "counts": got, "expected": expected, "failures": fails, "ok": not fails}, open(out, "w"), indent=1)
print(json.dumps({"counts": got, "failures": fails}))
sys.exit(1 if fails else 0)
'''

# ---------------------------------------------------------------- batch mode
# A batch app is launched by its own generator as a `flux batch` run.sh whose bulksubmit chunk jobs run <submit script> on compute nodes. The package renders a per-chunk wrapper
# that starts moirai-service in the background of the chunk job's own shell, so nothing is started on the login node.
WRAPPER_TMPL = r'''#!/bin/bash
# Per-chunk submit wrapper (rendered by 03_build.sh from submit_moirai.sh.tmpl). flux bulksubmit runs it on the worker node as: submit_moirai.sh <chunk csv>
# It starts moirai-service in the background of THIS shell, runs the original submit script as a child with its arguments unchanged, stops the service, and keeps the return code.
ORIG=@@ORIG@@
ENVF=@@ENV@@
STAGE=@@STAGE@@
MVENV=@@MVENV@@
[ -f "${ORIG}" ] || { echo "moirai-wrapper: original submit script ${ORIG} not found" >&2; exit 2; }
[ -f "${ENVF}" ] || { echo "moirai-wrapper: environment file ${ENVF} not found" >&2; exit 2; }
source "${ENVF}" || { echo "moirai-wrapper: sourcing ${ENVF} failed" >&2; exit 2; }
command -v moirai-service >/dev/null 2>&1 || { echo "moirai-wrapper: moirai-service not on PATH after sourcing ${ENVF}" >&2; exit 2; }
_job="${FLUX_JOB_ID:-$$}"
_host="$(hostname)"
export MOIRAI_LOG_FILE="${STAGE}/raw/prov"
export MOIRAI_DATA_DIR=@@DATA@@
export MOIRAI_SERVICE_SHM="/moirai-repro-${_job}"
export MOIRAI_PC_BLOCKS=${MOIRAI_PC_BLOCKS:-16}
export MOIRAI_PC_FLUSH_MS=${MOIRAI_PC_FLUSH_MS:-250}
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  for _c in "${MVENV}"/lib/python*/site-packages/dftracer/bin/dftracer_service; do
    if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
  done
  [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service under ${MVENV}" >&2; exit 2; }
fi
_svclog="${STAGE}/service-${_host}-${_job}.log"
echo "${_host}" >> "${STAGE}/hosts.txt"
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
for _i in $(seq 1 600); do [ -s "/dev/shm${MOIRAI_SERVICE_SHM}" ] && break; sleep 0.1; done
if [ ! -s "/dev/shm${MOIRAI_SERVICE_SHM}" ]; then
  echo "moirai-wrapper: ring /dev/shm${MOIRAI_SERVICE_SHM} not visible on ${_host} after 60 s; not running the app (service log ${_svclog})" >&2
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
# the batch job's return code does not reflect its chunk jobs: read its --output file (exit 4 chunk job failed, 5 no wrapper ran, 6 --output unreadable)
CHECK_BATCH = r'''#!/usr/bin/env python3
"""Judge a finished flux batch job from what it left behind: python stdlib only.
    check_batch.py <run.sh> <hosts.txt> <batch job id>
run.sh ends normally even when a bulksubmit chunk job fails, so its return code proves nothing. Reads the `#flux: --output <path>` file named in run.sh.
Exit 0 clean; 4 a chunk job failed (the output has `exited with exit code <non-zero>` or a line ending ` failed`); 5 hosts.txt missing or empty (no wrapper ran, nothing was traced);
6 the --output file is not readable."""
import os, re, sys
run_sh, hosts, jid = sys.argv[1:4]
m = re.search(r"^#flux:\s*--output(?:=|\s+)(\S+)", open(run_sh, errors="replace").read(), re.M)
out = m.group(1).strip("'\"") if m else ""
if out:
    if not os.access(out, os.R_OK):
        print(f"check_batch: batch job {jid} ended but its --output file {out} is not readable: cannot tell whether the chunk jobs ran", file=sys.stderr)
        sys.exit(6)
    lines = open(out, errors="replace").read().splitlines()
    failed = [l for l in lines if re.search(r" failed\s*$", l)]
    codes = [l for l in lines if re.search(r"exited with exit code [1-9][0-9]*", l)]
    if failed or codes:
        ids = " ".join(i.group(1) for i in (re.match(r"^.*: (\S+) failed\s*$", l) for l in failed) if i)
        print(f"check_batch: batch job {jid} returned 0 but a chunk job FAILED (chunk job id(s): {ids or 'unknown'}); run.sh ends normally even when a chunk job fails. Evidence in {out}:", file=sys.stderr)
        print("\n".join((codes + failed)[:10]), file=sys.stderr)
        sys.exit(4)
else:
    print("check_batch: run.sh has no '#flux: --output' header: only hosts.txt can be checked", file=sys.stderr)
if not (os.path.exists(hosts) and os.path.getsize(hosts) > 0):
    print(f"check_batch: batch job {jid} left no host in {hosts}: no per-chunk wrapper ran, so no service was started and nothing was traced", file=sys.stderr)
    sys.exit(5)
hs = sorted(set(open(hosts).read().split()))
print(f"check_batch: batch job {jid} clean: no failed chunk job in {out or '<no --output header>'}; hosts: {' '.join(hs)}")
'''
BATCH_STEPS = {
    "00_check_prereqs.sh": r'''# Commands, and that the runtime this package needs can be reached.
for c in git patch python3 flux; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 1; }; done
set +u
type module >/dev/null 2>&1 || { echo "the 'module' command is not defined: run from a login shell (bash -l)" >&2; exit 1; }
set -u
if [[ "$REUSE_VENV" == 1 ]]; then
  [[ -x "$PY" ]] || { echo "REUSE_VENV=1 but the app venv $VENV has no bin/python" >&2; exit 1; }
  "$MOIRAI_VENV/bin/python" -c "import moirai" || { echo "REUSE_VENV=1 but $MOIRAI_VENV has no moirai" >&2; exit 1; }
  [[ -x "$MOIRAI_VENV/bin/moirai-service" ]] || { echo "no moirai-service in $MOIRAI_VENV/bin" >&2; exit 1; }
fi
log "ok"
''',
    "02_setup_env.sh": r'''# Two environments: the moirai venv (dftracer, moirai) and the app venv (the app's own, with NO moirai in it). REUSE_VENV=1 uses both as they are.
require MOIRAI_VENV
MPY="$MOIRAI_VENV/bin/python"
if [[ "$REUSE_VENV" == 1 ]]; then
  log "reusing the app venv $VENV and the moirai venv $MOIRAI_VENV"
  log "NOT EXERCISED: the app environment build (see README: setup-env.sh without the emf_data step, a login-node build) and the moirai install"
  exit 0
fi
if [[ ! -x "$MPY" ]]; then
  python3 -m venv "$MOIRAI_VENV"
  "$MPY" -m pip install -q --upgrade pip scikit-build-core pybind11
  DFTRACER_WHEEL=1 "$MPY" -m pip install -q "$DFTRACER_SPEC"
  "$MPY" -m pip install -q --no-build-isolation "$MOIRAI_SPEC"
  [[ -z "$UTILS_SPEC" ]] || "$MPY" -m pip install -q "$UTILS_SPEC"
fi
if [[ ! -x "$PY" ]]; then
  [[ -n "$APP_SETUP_COMMAND" ]] || { echo "no app venv at $VENV and APP_SETUP_COMMAND is empty: build it as README says" >&2; exit 1; }
  (cd "$SRC" && bash -c "$APP_SETUP_COMMAND")
fi
[[ -x "$PY" ]] || { echo "the app venv $VENV still has no bin/python" >&2; exit 1; }
log "environments ready"
''',
    "03_build.sh": r'''# The moirai environment file, the shim directory and the per-chunk wrapper, rendered from scripts/submit_moirai.sh.tmpl. Nothing is compiled.
require MOIRAI_VENV SUBMIT_SCRIPT
MPY="$MOIRAI_VENV/bin/python"
SITE_M="$("$MPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
SHIM="$OUTPUT_ROOT/moirai_shim"
rm -rf "$SHIM"; mkdir -p "$SHIM"
for n in moirai dftracer dftracer.libs moirai.libs; do
  if [[ -e "$SITE_M/$n" ]]; then ln -s "$SITE_M/$n" "$SHIM/$n"; fi
done
[[ -e "$SHIM/moirai" && -e "$SHIM/dftracer" ]] || { echo "no moirai/dftracer under $SITE_M" >&2; exit 1; }
ENVF="$OUTPUT_ROOT/moirai_env.sh"
{
  echo "# Generated by 03_build.sh: the environment of the annotated app."
  echo "export MOIRAI_SERVICE=$MOIRAI_VENV/bin/moirai-service"
  echo "export PATH=\"\$PATH\":$MOIRAI_VENV/bin"
  echo "export PYTHONPATH=$SHIM:$SRC\"\${PYTHONPATH:+:\$PYTHONPATH}\""
  echo "export LD_LIBRARY_PATH=\"\${LD_LIBRARY_PATH:+\$LD_LIBRARY_PATH:}\"$SITE_M/moirai/lib64:$SITE_M/dftracer/lib64:$SITE_M/dftracer/lib"
} > "$ENVF"
ORIG="$SRC/$SUBMIT_SCRIPT"
[[ -f "$ORIG" ]] || { echo "the app's submit script $ORIG does not exist: wrong APP_REF or SUBMIT_SCRIPT" >&2; exit 1; }
STAGE_DIR="$OUTPUT_ROOT/traces/$SCALE"
sed -e "s|@@ORIG@@|$ORIG|g" -e "s|@@ENV@@|$ENVF|g" -e "s|@@STAGE@@|$STAGE_DIR|g" -e "s|@@MVENV@@|$MOIRAI_VENV|g" -e "s|@@DATA@@|$SRC|g" \
  "$PKG_DIR/scripts/submit_moirai.sh.tmpl" > "$OUTPUT_ROOT/submit_moirai.sh"
chmod 755 "$OUTPUT_ROOT/submit_moirai.sh"
bash -n "$OUTPUT_ROOT/submit_moirai.sh"
log "wrapper $OUTPUT_ROOT/submit_moirai.sh ready"
''',
    "04_run.sh": r'''# The run: make the input, generate the app's run.sh with the wrapper as its submit script, submit it as a flux batch, wait, and judge the chunk jobs.
# No service runs here: every chunk job's wrapper starts its own on its compute node.
require MOIRAI_VENV QUEUE INCHUNKS APP_CONFIG PRESTAGE APP_CWD MODULES
set +u
type module >/dev/null 2>&1 || { echo "the 'module' command is not defined: run from a login shell (bash -l)" >&2; exit 1; }
module load $MODULES || { echo "module load $MODULES failed" >&2; exit 1; }
set -u
command -v dbcast >/dev/null || { echo "dbcast is not on PATH after module load $MODULES" >&2; exit 1; }
command -v flux >/dev/null || { echo "flux is not on PATH" >&2; exit 1; }
WRAPPER="$OUTPUT_ROOT/submit_moirai.sh"
[[ -x "$WRAPPER" ]] || { echo "$WRAPPER is missing: run 03_build.sh first" >&2; exit 1; }
STAGE_DIR="$OUTPUT_ROOT/traces/$SCALE"
RUN="$OUTPUT_ROOT/work/$SCALE"
rm -rf "$STAGE_DIR" "$RUN"
mkdir -p "$TRACES" "$RUN"
if [[ "$SCALE" == smoke ]]; then
  first="$(ls -1 "$INCHUNKS" | sort | head -n 1)"
  [[ -n "$first" ]] || { echo "no chunk in $INCHUNKS" >&2; exit 1; }
  mkdir -p "$RUN/inchunks"
  head -n "${SMOKE_LINES:-2}" "$INCHUNKS/$first" > "$RUN/inchunks/$first"
  CHUNKS="$RUN/inchunks"
  log "smoke input: first ${SMOKE_LINES:-2} lines of $first"
else
  CHUNKS="$INCHUNKS"
fi
cd "$RUN"
"$PY" "$SRC/utils/gen_launch_script.py" --config "$SRC/$APP_CONFIG" --inchunks "$CHUNKS" --submit-script "$WRAPPER" --src-dir "$SRC" \
  --output-dir "$RUN/out" --env-dir "$VENV" --timing-dir "$RUN/timing" --log-dir "$RUN/logs" --prestage-script "$SRC/$PRESTAGE" \
  --overwrite --cwd "$SRC/$APP_CWD" -- run.sh
[[ -s "$RUN/run.sh" ]] || { echo "the generator wrote no $RUN/run.sh" >&2; exit 1; }
var="LIMIT_MIN_$SCALE"
MIN="${!var:-60}"
jid="$(flux batch -t "$MIN" --queue "$QUEUE" "$RUN/run.sh")" || { echo "flux batch submission failed" >&2; exit 1; }
log "batch job $jid ($SCALE, $MIN min, queue $QUEUE)"
cleanup() { trap - TERM INT; flux cancel "$jid" 2>/dev/null || true; exit 143; }
trap cleanup TERM INT
flux watch "$jid" || true
rc="$(flux jobs -n -o '{returncode}' "$jid" 2>/dev/null | tr -d ' ')" || true
res="$(flux jobs -n -o '{result}' "$jid" 2>/dev/null | tr -d ' ')" || true
log "batch job $jid result=$res returncode=$rc"
if [[ "$res" != "COMPLETED" || "$rc" != "0" ]]; then
  echo "batch job $jid FAILED (result=$res returncode=$rc): read its --output file, the chunk logs and the service logs in $STAGE_DIR" >&2
  exit 1
fi
python3 "$PKG_DIR/scripts/check_batch.py" "$RUN/run.sh" "$STAGE_DIR/hosts.txt" "$jid"
''',
}
for _k in ("01_get_source.sh", "05_validate.sh"):
    BATCH_STEPS[_k] = STEPS[_k]
BATCH_RUN_ALL = RUN_ALL


def _pins(py):
    r = subprocess.run([py, "-c", PIN], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def _patch(src, dst, files):
    out = ""
    for rel in files:
        r = subprocess.run(["diff", "-uN", "--label", f"a/{rel}", "--label", f"b/{rel}", str(src / rel), str(dst / rel)], capture_output=True, text=True)
        out += r.stdout
    return out


def _site(text):
    """Site values are written so the package is nobody's: home and user become variables."""
    home, user = os.path.expanduser("~"), os.environ.get("USER", "")
    text = text.replace(home, "${HOME}")
    return text.replace(f"/{user}/", "/${USER}/") if user else text


def _is_batch(*outs):
    return any(o and (o.get("mode") == "batch" or o.get("batch_script") or o.get("submit_command")) for o in outs)


def _minutes(t):
    """Pure: a flux duration ('1h', '30m', '90', '45s') in whole minutes; a bare number is minutes. 0 when unreadable."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", (t or "").strip())
    if not m:
        return 0
    return max(1, math.ceil(float(m.group(1)) * {"s": 1 / 60, "m": 1, "h": 60, "d": 1440, "": 1}[m.group(2)]))


def parse_gen(cmd, src=""):
    """Pure: the generator and submit flags of a pipeline command string (`...; python utils/gen_launch_script.py --config C --inchunks D ... -- run.sh; flux batch -t 1h --queue Q run.sh`).
    Returns {config, inchunks, submit_script, prestage, queue, minutes, setup}; a flag that is not there is ''. Paths under src become relative to it."""
    def flag(name):
        m = re.search(rf"(?<![\w-]){re.escape(name)}(?:=|\s+)(\S+)", cmd or "")
        return m.group(1).strip("'\"") if m else ""

    def rel(v):
        v = v.split("<src>/", 1)[-1] if "<src>/" in v else v
        return v[len(src) + 1:] if src and v.startswith(src.rstrip("/") + "/") else v
    t = re.search(r"flux batch\s+(?:\S+\s+)*?-t\s+(\S+)", cmd or "")
    s = re.search(r"(\./setup-env\.sh[^;]*)", cmd or "")
    return {"config": rel(flag("--config")), "inchunks": flag("--inchunks"), "submit_script": rel(flag("--submit-script")), "prestage": rel(flag("--prestage-script")),
            "queue": flag("--queue"), "minutes": _minutes(t.group(1)) if t else 0,
            "setup": re.sub(r"\s*<venv>\s*$", " ${VENV}", s.group(1).strip().replace("<venv>", "${VENV}")) if s else ""}


def _batch_cfg(app, output_root, url, ref, pins, cmd, src=""):
    """Pure: config.ini text of a batch package and its warnings. Only ${USER}/${HOME} variables, no session, user or job path."""
    g, warnings = parse_gen(cmd, src), []
    for k in ("config", "inchunks", "submit_script", "prestage"):
        if not g[k]:
            warnings.append(f"could not read --{k.replace('_', '-')} from the command: fill {k.upper()} in config.ini")
    if not re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        warnings.append("APP_REF is not a full 40-character commit: pin it in config.ini")
    for n in ("dftracer", "moirai"):
        if n not in pins:
            warnings.append(f"could not pin {n}: fill the SPEC lines in config.ini")
    if g["setup"]:
        warnings.append("APP_SETUP_COMMAND is the pipeline's setup-env.sh line; the app env build is not exercised by repro_verify (README: remove its emf_data step, build on a login node)")
    cwd = (os.path.dirname(g["submit_script"]) or "monomer-structure-prediction") if g["submit_script"] else "monomer-structure-prediction"
    lines = ["# The ONLY file with site or user values. Every script sources scripts/lib_load_config.sh, which reads it. The environment overrides it.", "",
             "[paths]", f"APP={app}", f"OUTPUT_ROOT={_site(output_root)}", "# the app's own venv (no moirai in it) and the moirai venv", "VENV=${OUTPUT_ROOT}/env",
             "MOIRAI_VENV=${OUTPUT_ROOT}/moirai-venv", "# 1 uses both existing venvs as they are.", "REUSE_VENV=0", "",
             "[repos]", f"APP_URL={url}", f"APP_REF={ref}", f"DFTRACER_SPEC={pins.get('dftracer', '')}", f"MOIRAI_SPEC={pins.get('moirai', '')}",
             f"UTILS_SPEC={pins.get('dftracer-utils', '')}", "", "[run]", "# batch: the app is launched as a flux batch; smoke or baseline.", "MODE=batch", "SCALE=smoke",
             f"INCHUNKS={g['inchunks']}", "# smoke input: the first SMOKE_LINES lines of the first chunk", "SMOKE_LINES=2", f"APP_CONFIG={g['config']}",
             f"SUBMIT_SCRIPT={g['submit_script']}", f"PRESTAGE={g['prestage']}", f"APP_CWD={cwd}", f"QUEUE={g['queue'] or 'pdebug'}",
             "# flux batch time limits in minutes", "LIMIT_MIN_smoke=30", f"LIMIT_MIN_baseline={max(g['minutes'], 60)}", "MODULES=mpifileutils/0.11.1",
             f"APP_SETUP_COMMAND={g['setup']}", "BUILD_COMMAND=", ""]
    return "\n".join(lines), warnings


def lint(pkg, ws):
    findings = []
    home, user = os.path.expanduser("~"), os.environ.get("USER", "")
    forbidden = {str(ws): "workspace path", home: "home path"}
    if user:
        forbidden[f"/{user}/"] = "username in a path"
    for f in sorted(pkg.glob("scripts/*")):
        text = f.read_text(errors="replace")
        findings += [f"{f.name}: hard-coded {what}" for needle, what in forbidden.items() if needle and needle in text]
        if JOBID.search(text):
            findings.append(f"{f.name}: looks like a job id")
        if f.suffix == ".sh":
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            if r.returncode:
                findings.append(f"{f.name}: bash -n: {r.stderr.strip()[:160]}")
            if f.name != LOADER and LOADER not in text:
                findings.append(f"{f.name}: does not source {LOADER}")
    cfg = pkg / "config.ini"
    if not cfg.exists():
        return findings + ["config.ini is missing"]
    text = cfg.read_text()
    findings += [f"config.ini: hard-coded {what}" for needle, what in forbidden.items() if needle and needle in text]
    if user and re.search(rf"(?<![\w$.{{]){re.escape(user)}(?!\w)", text):
        findings.append("config.ini: literal username: use ${USER}")
    return findings


def _dir(c):
    return P.work(c).parent / "final_provenance" if c.get("session") else P.pdir(c["id"]) / "final_provenance"


def _bash_n(text):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.sh"
        f.write_text(text)
        r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        return r.returncode == 0, r.stderr.strip()[:200]


# literal values that must not appear in a rendered script, assembled from parts so this file holds no machine path itself
_LITERAL = re.compile("|".join(["/" + "usr/WS2", "/" + r"home/\w", "/g/g" + r"\d", "hari" + "dev", "2026" + r"\d{4}_", "moirai-agents-prov", "openfold/" + "2026"]))
_SETUP = "." + "/setup-env.sh --use-rocm7 --no-managed-python"
_SPLITS = "/" + "p/lustre5/lbann/elmerfold/splits/1n_10s"


def _selftest():
    """Pure renders and the failure-pattern logic on real text; nothing touches a cluster."""
    cases, notes = {}, []
    # wrapper template
    w = WRAPPER_TMPL
    for k, v in {"@@ORIG@@": "/o/submit.sh", "@@ENV@@": "/o/env.sh", "@@STAGE@@": "/o/traces/smoke", "@@MVENV@@": "/o/mv", "@@DATA@@": "/o/src"}.items():
        w = w.replace(k, v)
    ok, err = _bash_n(w)
    cases["wrapper_renders"] = ok and "@@" not in w and "moirai-service start" in w and "MOIRAI_DFTRACER_SERVICE" in w and 'hosts.txt' in w and 'bash "${ORIG}" "$@"' in w and "exit ${rc}" in w
    if not ok:
        notes.append(err)
    # 04_run text: both scales are one script selected by SCALE; it must carry every generator flag, the module, the batch, the watch and the check, and no service
    t = BATCH_STEPS["04_run.sh"]
    flags = ["--config", "--inchunks", "--submit-script", "--src-dir", "--output-dir", "--env-dir", "--timing-dir", "--log-dir", "--prestage-script", "--overwrite", "--cwd"]
    cases["run_text"] = (all(f in t for f in flags) and "module load $MODULES" in t and "flux batch -t" in t and "--queue" in t and "flux watch" in t and "check_batch.py" in t
                         and "LIMIT_MIN_$SCALE" in t and "SMOKE_LINES" in t and "moirai-service" not in t and "eval" not in t and _bash_n("source /dev/null\n" + t)[0])
    # failure patterns on the real failing run.out text and a clean one
    out_fail = "flux-job: task(s) exited with exit code 2\nFri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\nflux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n"
    out_ok = "Fri Oct 30 23:30:01 PDT 2026: f35QqToh9 done\nflux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n"
    out_code = "flux-job: task(s) exited with exit code 1\n"
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "chk.py").write_text(CHECK_BATCH)
        hosts, hempty = d / "hosts.txt", d / "empty.txt"
        hosts.write_text("node1\nnode1\n")
        hempty.write_text("")

        def run(out_text, hfile, header=True):
            o = d / "run.out"
            if out_text is not None:
                o.write_text(out_text)
            elif o.exists():
                o.unlink()
            rs = d / "run.sh"
            rs.write_text("#!/bin/bash\n" + (f"#flux: --output={o}\n" if header else "") + "echo hi\n")
            r = subprocess.run([sys.executable, str(d / "chk.py"), str(rs), str(hfile), "jobX"], capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr
        rc, msg = run(out_fail, hosts)
        cases["fail_text_exit4"] = rc == 4 and "f35QqToh9" in msg
        cases["exit_code_only_exit4"] = run(out_code, hosts)[0] == 4
        rc, msg = run(out_ok, hosts)
        cases["clean_exit0"] = rc == 0 and "clean" in msg
        cases["empty_hosts_exit5"] = run(out_ok, hempty)[0] == 5 and run(out_ok, d / "missing.txt")[0] == 5
        cases["unreadable_exit6"] = run(None, hosts)[0] == 6
        cases["no_header_checks_hosts"] = run(out_fail, hosts, header=False)[0] == 0 and run(out_fail, hempty, header=False)[0] == 5
    # parse of the pipeline command and the rendered config
    cmd = (_SETUP + " <venv>; python utils/gen_launch_script.py --config configs/tuolumne_test.yaml --inchunks " + _SPLITS + " "
           "--submit-script <src>/monomer-structure-prediction/submit_smsp.sh --output-dir <out> --env-dir <venv> --timing-dir test_timing --log-dir test_logs "
           "--prestage-script <src>/monomer-structure-prediction/prestage_check.sh -- run.sh; flux batch -t 1h --queue pdebug run.sh")
    g = parse_gen(cmd)
    cases["parse_gen"] = (g["config"] == "configs/tuolumne_test.yaml" and g["inchunks"].endswith("1n_10s") and g["submit_script"] == "monomer-structure-prediction/submit_smsp.sh"
                          and g["prestage"] == "monomer-structure-prediction/prestage_check.sh" and g["queue"] == "pdebug" and g["minutes"] == 60 and g["setup"].endswith("${VENV}") and "<" not in g["setup"])
    cases["minutes"] = [_minutes(x) for x in ("1h", "30m", "90", "45s", "x")] == [60, 30, 90, 1, 0]
    ref = "0123456789abcdef0123456789abcdef01234567"
    cfg, warns = _batch_cfg("app", "${HOME}/x/${USER}", "ssh://git@host/g/r.git", ref, {"dftracer": "dftracer==1", "moirai": "moirai==1"}, cmd)
    cases["config_renders"] = ("MODE=batch" in cfg and f"APP_REF={ref}" in cfg and "LIMIT_MIN_baseline=60" in cfg and "<" not in cfg and "${USER}" in cfg
                               and f"APP_SETUP_COMMAND={_SETUP} ${{VENV}}" in cfg)
    # no rendered script holds a user, workspace, session or job path
    texts = {"wrapper": w, "check_batch": CHECK_BATCH, "lib": LIB, **{k: v for k, v in BATCH_STEPS.items()}, "config": cfg}
    bad = [k for k, v in texts.items() if _LITERAL.search(v) or (k != "wrapper" and JOBID.search(v))]
    cases["no_literal_paths"] = not bad
    if bad:
        notes.append(f"literal values in {bad}")
    cases["scripts_parse"] = all(_bash_n(LIB + "\n" + BATCH_STEPS[k])[0] for k in BATCH_STEPS)
    line = "repro batch selftest PASS " + " ".join(f"{k}=ok" for k in cases) if all(cases.values()) else "repro batch selftest FAIL " + ",".join(k for k, v in cases.items() if not v)
    return {"status": "ok" if all(cases.values()) else "error", "line": line, "cases": cases, "notes": notes}


def register(mcp):
    @mcp.tool()
    def repro_build(pipeline: str, output_root: str = "${HOME}/moirai_repro/${APP}", mode: str = "") -> dict:
        """Generate final_provenance/ in the session: config.ini, scripts, the provenance patch, the spec, pinned versions and a standalone checker. Use after the query stage, or again after
        a fix: it starts over. output_root: where a reader's run writes; keep ${USER} or ${HOME}, never a literal name. mode: '' detects it (batch when the moirai_smoke or baseline stage ran a
        generated flux batch run.sh: its outputs hold batch_script or submit_command), 'batch' or 'generic' forces it. Batch mode writes a per-chunk wrapper template, a run.sh generator call, a
        flux batch with a wait and failure checks, and never starts a service on the login node. Returns {status, package, mode, files, source, pins, warnings}."""
        c = P.load(pipeline)
        integ, ann, base = P.stage(c, "integrate")["outputs"], P.stage(c, "annotate")["outputs"], P.stage(c, "baseline")["outputs"]
        smoke_run = P.stage(c, "moirai_smoke")["outputs"]
        if not ann.get("spec") or not smoke_run.get("traces"):
            return {"status": "error", "error": "needs the annotate and moirai_smoke stages"}
        if mode not in ("", "auto", "batch", "generic"):
            return {"status": "error", "error": "mode is batch, generic or empty"}
        batch = mode == "batch" or (mode in ("", "auto") and _is_batch(smoke_run, base))
        pkg, src, dst = _dir(c), Path(P.source_of(c)), Path(ann["annotated"])
        shutil.rmtree(pkg, ignore_errors=True)
        for d in ("scripts", "patches", "inputs"):
            (pkg / d).mkdir(parents=True)
        warnings = []
        (pkg / "patches" / "provenance.patch").write_text(_patch(src, dst, list(ann["files"]) + (["_moirai_prov.py"] if (dst / "_moirai_prov.py").exists() else [])))
        (pkg / "inputs" / "spec.json").write_text(json.dumps(ann["spec"], indent=1))
        expected = {"smoke": {k: v for k, v in T.read(smoke_run["traces"]).counts().items() if k in ("activities", "entities", "edges")}}
        if base.get("traces"):
            expected["baseline"] = {k: v for k, v in T.read(base["traces"]).counts().items() if k in ("activities", "entities", "edges")}
        (pkg / "inputs" / "expected.json").write_text(json.dumps(expected, indent=1))
        url, ref = "", ""
        if re.match(r"^(https?://|git@|ssh://)", c["source"]):
            url = c["source"]
            r = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True)
            ref = r.stdout.strip()
        else:
            with tarfile.open(pkg / "inputs" / "source.tar.gz", "w:gz") as tar:
                for f in sorted(src.iterdir()):
                    if f.name not in {".git", "__pycache__", "build"}:
                        tar.add(f, arcname=f.name)
            mb = (pkg / "inputs" / "source.tar.gz").stat().st_size >> 20
            if mb > 50:
                warnings.append(f"the source bundle is {mb} MiB: prefer a git URL")
        pins = _pins(M.python())
        missing = [n for n in ("dftracer", "moirai") if n not in pins]
        if missing:
            warnings.append(f"could not pin {missing}: fill the SPEC lines in config.ini")
        app = re.sub(r"\W", "_", c["name"])
        if batch:
            cfg_text, w2 = _batch_cfg(app, output_root, url, ref, pins, base.get("command") or smoke_run.get("command") or c["command"], str(src))
            warnings += [x for x in w2 if x not in warnings]
            steps, run_all = BATCH_STEPS, BATCH_RUN_ALL
            (pkg / "scripts" / "submit_moirai.sh.tmpl").write_text(WRAPPER_TMPL)
            (pkg / "scripts" / "check_batch.py").write_text(CHECK_BATCH)
        else:
            cfg = ["# The ONLY file with site or user values. Every script sources scripts/lib_load_config.sh, which reads it. The environment overrides it.", "", "[paths]",
                   f"APP={app}", f"OUTPUT_ROOT={_site(output_root)}", "VENV=${OUTPUT_ROOT}/env", "# 1 uses an existing venv with moirai in it.", "REUSE_VENV=0", "", "[repos]",
                   f"APP_URL={url}", f"APP_REF={ref}", f"DFTRACER_SPEC={pins.get('dftracer', '')}", f"MOIRAI_SPEC={pins.get('moirai', '')}", f"UTILS_SPEC={pins.get('dftracer-utils', '')}", "", "[run]",
                   "# smoke or baseline.", "SCALE=smoke", f"COMMAND_smoke={_site(smoke_run.get('command') or c['command'])}", f"COMMAND_baseline={_site(base.get('command') or c['command'])}",
                   f"LAUNCHER={base.get('launcher', '')}", f"BUILD_COMMAND={_site(integ.get('build_command') or '')}", ""]
            cfg_text, steps, run_all = "\n".join(cfg), STEPS, RUN_ALL
        (pkg / "config.ini").write_text(cfg_text)
        (pkg / "scripts" / LOADER).write_text(LIB)
        (pkg / "scripts" / "prov_check.py").write_text(CHECK)
        head = '#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n'
        for name, body in steps.items():
            (pkg / "scripts" / name).write_text(head + body)
        (pkg / "scripts" / "run_all.sh").write_text(head + run_all)
        for f in (pkg / "scripts").iterdir():
            f.chmod(0o755)
        counts = expected.get("baseline") or expected["smoke"]
        if batch:
            readme = f"""# {c['name']}: provenance reproduction package (batch mode)

Reproduces the moirai provenance of {c['name']}: {counts['activities']} activities and {counts['entities']} entities in the original run.
Scientific artifacts: {', '.join(ann['spec'].get('scientific_artifacts', []))}.

The app is launched by its own generator as a `flux batch` job (2 exclusive nodes; one `flux bulksubmit` chunk job per chunk on the worker node). The generator is given a per-chunk wrapper as its
submit script; the wrapper starts `moirai-service` in the background of the chunk job's own shell, runs the original submit script, and stops the service. No service runs on the login node.

## Use

```bash
$EDITOR config.ini            # the only file with site values
bash -l scripts/run_all.sh    # a login shell: it needs the `module` command and flux; SCALE=baseline for the full split
```

Smoke = the first `SMOKE_LINES` lines of the first chunk of `INCHUNKS`; baseline = all of `INCHUNKS`. Each takes about 8 minutes, dominated by a ~270 s dbcast broadcast (`mpifileutils`).
`04_run.sh` submits the batch job, waits with `flux watch`, then `scripts/check_batch.py` fails the run when the batch `--output` file holds `exited with exit code <non-zero>` or a line ending
` failed` (a batch job's own return code does not reflect its chunk jobs), or when `hosts.txt` is empty. `scripts/prov_check.py` then compares the counts with `inputs/expected.json`.

## The two environments

- The moirai venv (`MOIRAI_VENV`: dftracer, moirai at the pinned versions) is built by `02_setup_env.sh`. The app never installs moirai: `03_build.sh` writes `moirai_env.sh` and a shim directory of symlinks (moirai, dftracer) that go on PYTHONPATH.
- The app venv (`VENV`) is the app's own. `APP_SETUP_COMMAND` is the pipeline's `setup-env.sh` line, but the app's `setup-env.sh` must have its `emf_data` step removed first (it needs data this package cannot fetch), it must be built on a
  login node (hours), and compute nodes cannot create files under any `python3.N/site-packages`, so nothing may be installed into the venv at run time. Build it once, then use `REUSE_VENV=1`.
  `repro_verify` always reuses both venvs: the full environment build has NOT been exercised.

| Script | Does |
|---|---|
| `00_check_prereqs.sh` | commands, `module`, and both venvs when reused |
| `01_get_source.sh` | the app at its pinned commit plus `patches/provenance.patch` |
| `02_setup_env.sh` | the moirai venv and the app venv; `REUSE_VENV=1` skips both |
| `03_build.sh` | `moirai_env.sh`, the shim and the wrapper rendered from `scripts/submit_moirai.sh.tmpl` |
| `04_run.sh` | smoke input, `run.sh` from the generator, `flux batch`, `flux watch`, the failure checks |
| `05_validate.sh` | the provenance check against `inputs/expected.json` |
"""
        else:
            readme = f"""# {c['name']}: provenance reproduction package

Reproduces the moirai provenance of {c['name']}: {counts['activities']} activities and {counts['entities']} entities in the original run.
Scientific artifacts: {', '.join(ann['spec'].get('scientific_artifacts', []))}.

## Use

```bash
$EDITOR config.ini            # the only file with site values
bash scripts/run_all.sh       # SCALE=baseline for the full run
```

`scripts/run_all.sh` runs the numbered scripts in order, and each can be run alone. The result is judged by `scripts/prov_check.py`, which needs only Python, and written to `$OUTPUT_ROOT/run.json`.

| Script | Does |
|---|---|
| `00_check_prereqs.sh` | commands and access |
| `01_get_source.sh` | the app at its pinned state, plus `patches/provenance.patch` |
| `02_setup_env.sh` | the runtime at the pinned versions; `REUSE_VENV=1` skips it |
| `03_build.sh` | the patched app against moirai |
| `04_run.sh` | the run under `moirai-service` |
| `05_validate.sh` | the provenance check against `inputs/expected.json` |
"""
        (pkg / "README.md").write_text(readme)
        files = sorted(str(f.relative_to(pkg)) for f in pkg.rglob("*") if f.is_file())
        P.record(pipeline, STAGE, status="running", outputs={"package": str(pkg), "mode": "batch" if batch else "generic", "files": len(files), "pins": pins, "warnings": warnings, "verified": None,
                                                             "summary": f"{len(files)} files in final_provenance ({'batch' if batch else 'generic'} mode)"}, note="package built")
        return {"status": "ok", "package": str(pkg), "mode": "batch" if batch else "generic", "files": files, "source": url or "bundled", "pins": pins, "warnings": warnings}

    @mcp.tool()
    def repro_verify(pipeline: str, isolation_root: str, scale: str = "smoke", reuse_venv: str = "", limit: int = 3600, wait: int = 300, moirai_venv: str = "") -> dict:
        """Prove the package works: copy it to a new folder outside the session and run it in a clean login shell, with nothing inherited (env -i, bash -l). Use after repro_build. isolation_root:
        a parent folder outside the session, on a file system a compute node can see. scale: smoke or baseline. reuse_venv: a venv to reuse instead of building one (batch mode: the app's own venv).
        moirai_venv: batch mode only, the moirai venv to reuse (needed with reuse_venv). limit: seconds for the whole run; in batch mode the flux batch time limit is derived from it. wait: seconds to
        wait now; the run goes on in the background (a batch run takes ~8 min) and repro_validate judges it later. Batch mode never starts a service on the login node.
        Returns {status, job, iso_dir, mode, not_exercised, log_tail}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        batch = o.get("mode") == "batch"
        if batch and bool(reuse_venv) != bool(moirai_venv):
            return {"status": "error", "error": "batch mode reuses both venvs or neither: give reuse_venv (the app venv) and moirai_venv together"}
        if scale not in ("smoke", "baseline"):
            return {"status": "error", "error": "scale is smoke or baseline"}
        root = Path(os.path.expanduser(isolation_root)).resolve()
        if str(root).startswith(str(P.work(c).parent.resolve()) + os.sep):
            return {"status": "error", "error": "isolation_root must be outside the session workspace"}
        iso = root / f"isolation_{time.strftime('%Y%m%d_%H%M%S')}"
        iso.mkdir(parents=True)
        shutil.copytree(o["package"], iso / "pkg", symlinks=True)
        env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
        env.update(TERM="dumb", OUTPUT_ROOT=str(iso / "out"), SCALE=scale)
        not_exercised = []
        if reuse_venv:
            env.update(REUSE_VENV="1", VENV=reuse_venv)
            not_exercised.append("environment build" if not batch else "app environment build and moirai install")
        if batch:
            if moirai_venv:
                env["MOIRAI_VENV"] = moirai_venv
            env[f"LIMIT_MIN_{scale}"] = str(max(1, int(limit) // 60 - 2))
        cmd = f"cd {shlex.quote(str(iso))} && bash pkg/scripts/run_all.sh"
        args = " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
        j = J.start(pipeline, STAGE, "isolated-run", f"env -i {args} bash -lc {shlex.quote(cmd)}\n", str(iso), limit=limit)
        P.record(pipeline, STAGE, outputs={"verify": {"job": j["id"], "iso_dir": str(iso), "scale": scale, "mode": "batch" if batch else "generic", "not_exercised": not_exercised}},
                 note=f"isolated run of {scale} in {iso}")
        r = J.wait(pipeline, j["id"], wait)
        return {"status": r["status"], "job": j["id"], "rc": r["rc"], "iso_dir": str(iso), "mode": "batch" if batch else "generic", "not_exercised": not_exercised, "log_tail": r.get("log_tail", "")[-2500:]}

    @mcp.tool()
    def repro_validate(pipeline: str) -> dict:
        """Validate the package: it holds no user, workspace or job path, its scripts parse and source the loader, a batch package starts no service in 04_run.sh, and an isolated run passed with the
        original counts. Use before the last gate. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if not v.check("built", bool(o.get("package")), "repro_build has not run", "run repro_build"):
            return P.finish_validation(pipeline, v)
        found = lint(Path(o["package"]), P.work(c).parent.resolve())
        v.check("portable", not found, "; ".join(found)[:380], "move the value to config.ini with ${HOME} or ${USER}, then repro_build")
        v.check("pinned", bool(o.get("pins", {}).get("moirai")), f"pins: {o.get('pins')}", "pin moirai and dftracer in config.ini")
        if o.get("mode") == "batch":
            run = Path(o["package"]) / "scripts" / "04_run.sh"
            text = run.read_text() if run.exists() else ""
            v.check("no_login_service", bool(text) and "moirai-service" not in text and "check_batch.py" in text and "flux watch" in text,
                    "04_run.sh starts a service, or does not wait for and check the batch job", "the service belongs to the per-chunk wrapper; update repro_package and repro_build")
        vf = o.get("verify")
        if not vf:
            v.inconclusive("isolated_run", "repro_verify has not run")
            return P.finish_validation(pipeline, v)
        j = J.get(pipeline, vf["job"], 1500)
        if j["status"] == "running":
            v.inconclusive("isolated_run", "still running")
            return P.finish_validation(pipeline, v)
        v.check("isolated_run", j["rc"] == 0, f"rc={j['rc']}: {j.get('log_tail', '')[-300:]}", "read the log: something the session provided is missing from the package")
        run = Path(vf["iso_dir"]) / "out" / "run.json"
        if v.check("reproduced", run.exists() and json.loads(run.read_text()).get("ok"), run.read_text()[:300] if run.exists() else "no run.json", "the counts differ: is the app deterministic? Is the input the same?"):
            note = f"; NOT exercised: {', '.join(vf['not_exercised'])}" if vf.get("not_exercised") else ""
            P.record(pipeline, STAGE, outputs={"summary": f"reproduced in a clean shell ({vf.get('scale')}): {json.loads(run.read_text())['counts']}{note}"})
        return P.finish_validation(pipeline, v)

    @mcp.tool()
    def repro_batch_selftest() -> dict:
        """Check the batch mode on pure renders, with no cluster: the per-chunk wrapper template, the 04_run text (both scales), the chunk-failure patterns on the real failing run.out text and a clean
        one, the command parse and config render, and that no script holds a user, workspace, session or job path. Use after changing repro_package. Returns {status, line, cases, notes}."""
        return _selftest()
