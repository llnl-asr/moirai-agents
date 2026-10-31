"""Last stage of the run pipeline: final_provenance/, a folder that reproduces the run for someone who has neither this session nor this account, and the proof that it does.

Same shape as dftracer-agents' reproduction packages: config.ini is the only file with site values, every script sources lib_load_config.sh, the instrumentation is a patch
against a pinned app, and a standalone checker judges the result. It is verified by running it from a fresh copy in a clean login shell.

Two modes. generic: the app runs in the shell that runs 04_run.sh, under a moirai-service started there. batch: the app is launched by its own generator as a `flux batch` whose chunk jobs run on
compute nodes; each chunk job runs a wrapper that starts its own moirai-service, so 04_run.sh never starts a service on the login node and waits for and judges the batch job.

Publishing: provenance-repro/<app>/ holds the package (run AS IT IS from there to verify it), provenance-traces/<app>/<job start>/ holds one run each (raw traces, README.md, run.json,
run.sh, graph_summary.json), then the site's chgperm.sh sets the group. Every publishing step takes publish_root as an argument and refuses any path outside it.
"""
import grp
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
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


# ------------------------------------------------------------------ publishing (pure cores take every path as an argument; the tools wrap them)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TS_RE = re.compile(r"^\d{8}_\d{6}$")
SKIP_DIRS = {".chgperm_logs"}
KINDS = ("baseline", "verification")
PKG_FILES = ("README.md", "config.ini", "scripts/run_all.sh")


def _inside(root, p):
    """True when p resolves to root or below it."""
    r, q = Path(root).resolve(), Path(p).resolve()
    return q == r or r in q.parents


def _names(app, ts=None):
    if not NAME_RE.match(app or ""):
        raise ValueError(f"app {app!r} must match {NAME_RE.pattern}")
    if ts is not None and not TS_RE.match(ts or ""):
        raise ValueError(f"timestamp {ts!r} must be YYYYMMDD_HHMMSS")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tree_hashes(d):
    """{relative path: sha256 (or link:<target>)} of every file under d, skipping chgperm's log folders."""
    out = {}
    for dp, dn, fn in os.walk(d):
        dn[:] = sorted(x for x in dn if x not in SKIP_DIRS)
        for f in fn:
            p = Path(dp) / f
            out[str(p.relative_to(d))] = "link:" + os.readlink(p) if p.is_symlink() else _sha(p)
    return out


def _du(d):
    return sum((Path(dp) / f).lstat().st_size for dp, _, fn in os.walk(d) for f in fn)


def _stamp(epoch):
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(float(epoch)))


def _flux_field(job, fmt):
    """One `flux jobs -n -o <fmt> <job>` query (read-only); '' when flux or the job is unavailable."""
    try:
        r = subprocess.run(["flux", "jobs", "-n", "-o", fmt, job], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _find_flux_id(text):
    """The flux batch id in a log: 04_run.sh logs 'batch job <id> (<scale>, ...)'."""
    m = re.search(r"batch job:?\s+(f[1-9A-Za-z]{9,})", text or "")
    if m:
        return m.group(1)
    m = JOBID.search(text or "")
    return m.group(0) if m else ""


def cfg_values(path, env):
    """Pure: config.ini values with ${VAR} expanded; env overrides the file, as lib_load_config.sh does."""
    vals = {}
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if not s or s[0] in "#[":
            continue
        k, _, v = s.partition("=")
        vals[k.strip()] = v
    scope = {**vals, **env}

    def ex(t):
        for _ in range(8):
            n = re.sub(r"\$\{(\w+)\}", lambda m: scope.get(m.group(1), m.group(0)), t)
            if n == t:
                break
            t = n
        return t
    return {k: ex(scope[k]) for k in vals}


def plan_core(src_pkg, root, app, stamps, group_script, group="moirai"):
    """Pure (reads only): the publish tree. stamps = {'baseline': ts or '', 'verification': ts or ''}. Returns {status, problems, ...}."""
    problems = []
    try:
        _names(app)
    except ValueError as e:
        return {"status": "error", "problems": [str(e)]}
    root = Path(root)
    if not root.is_dir():
        problems.append(f"publish_root {root} is not a directory")
    if not Path(src_pkg).is_dir():
        problems.append(f"package {src_pkg} is missing: run repro_build")
    pkg_t = root / "provenance-repro" / app
    targets = {"package": str(pkg_t)}
    existing = [str(pkg_t)] if pkg_t.exists() else []
    runs = []
    for kind in KINDS:
        ts = stamps.get(kind) or ""
        if not ts:
            runs.append({"kind": kind, "timestamp": "", "target": "", "note": "unknown yet"})
            continue
        if not TS_RE.match(ts):
            problems.append(f"{kind} timestamp {ts!r} is not YYYYMMDD_HHMMSS")
            continue
        t = root / "provenance-traces" / app / ts
        runs.append({"kind": kind, "timestamp": ts, "target": str(t), "gets": ["raw/*.pfw.gz", "raw/.dftindex (if present)", "README.md", "run.json", "run.sh", "graph_summary.json"] + (["verification.json"] if kind == "verification" else [])})
        if t.exists():
            existing.append(str(t))
    if existing:
        problems.append("targets exist, nothing will be overwritten: " + ", ".join(existing))
    if stamps.get("baseline") and stamps.get("baseline") == stamps.get("verification"):
        problems.append("baseline and verification have the same timestamp")
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free if probe.exists() else 0
    size = _du(src_pkg) if Path(src_pkg).is_dir() else 0
    gs = Path(group_script)
    if not _inside(root, gs):
        problems.append(f"group_script {gs} is outside publish_root")
    elif not gs.is_file():
        problems.append(f"group_script {gs} not found")
    dirs = [str(pkg_t)] + [r["target"] for r in runs if r["target"]]
    cmds = [f"{shlex.quote(str(gs))} --local -q -g {group} {shlex.quote(d)}" for d in dirs]
    return {"status": "error" if problems else "ok", "problems": problems, "targets": targets, "runs": runs, "existing": existing, "package_bytes": size, "free_bytes": free,
            "chgperm_commands": cmds, "unknown_timestamps": [r["kind"] for r in runs if not r["timestamp"]]}


def copy_package_core(src, root, app, ws=None):
    """cp -a src to <root>/provenance-repro/<app> (new dir only), compare hashes with src, run the portable check on the copy."""
    _names(app)
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"publish_root {root} is not a directory")
    tgt = root / "provenance-repro" / app
    if not _inside(root, tgt):
        raise ValueError("target is outside publish_root")
    if tgt.exists():
        raise FileExistsError(f"{tgt} exists: refusing to overwrite")
    tgt.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cp", "-a", str(src), str(tgt)], capture_output=True, text=True)
    if r.returncode:
        shutil.rmtree(tgt, ignore_errors=True)
        raise OSError("cp -a failed: " + r.stderr[:300])
    a, b = tree_hashes(src), tree_hashes(tgt)
    diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    portable = lint(tgt, Path(ws) if ws else Path("/nonexistent-session"))
    return {"target": str(tgt), "files": len(b), "hash_mismatch": diff, "portable_findings": portable, "ok": not diff and not portable}


def _metaval(meta, k):
    v = meta.get(k)
    return str(v) if v not in (None, "") else "not recorded"


def render_run_files(kind, app, ts, s, info, meta):
    """Pure: README.md, run.json and graph_summary.json texts from a graph summary dict (Graph.summary()), file info and metadata. Nothing is typed in: a missing fact reads 'not recorded'."""
    types = {t["type"]: {"role": t.get("role", ""), "description": t.get("description", "")} for t in s.get("types", [])}
    by_type, by_store, rel = s.get("by_type", {}), s.get("by_store", {}), s.get("relation_kinds", {})
    secs = meta.get("seconds")
    run = {"kind": kind, "jobid": _metaval(meta, "job_id"), "queue": _metaval(meta, "queue"), "result": _metaval(meta, "result"), "start": _metaval(meta, "start_iso"),
           "runtime_s": secs if secs not in (None, "") else "not recorded", "scale": _metaval(meta, "scale"), "input": _metaval(meta, "input"), "timestamp": ts}
    doc = {"app": {"name": app, "source_commit": _metaval(meta, "app_commit")}, "run": run,
           "software": {"dftracer": _metaval(meta, "dftracer"), "moirai": _metaval(meta, "moirai")}, "env": meta.get("env") or {},
           "captured": {"trace_files": info["files"], "trace_bytes": info["bytes"], "record_counts": {"events_total": s.get("records", 0), "other_events": s.get("other_events", {})},
                        "graph": {"entities": s["entities"], "activities": s["activities"], "edges": s["edges"], "relations": s.get("relations", 0), "dangling_hashes": s["dangling"], "types_described": len([t for t in types.values() if t["description"]])},
                        "entities_by_type": by_type, "entity_relations": rel, "entities_by_store": by_store, "entity_types": types}}
    if "verification" in meta:
        doc["verification"] = meta["verification"]
    graph = {"summary": {k: v for k, v in s.items() if k not in ("types", "relation_kinds")}, "entity_relations": rel, "types": s.get("types", [])}
    what = "the pipeline's baseline run" if kind == "baseline" else "the in-place verification run of the published package"
    L = [f"# {app} provenance trace -- {ts}", "",
         f"{kind.capitalize()} run ({what}), traced with the dftracer entity/relation API. Job {run['jobid']} ({run['queue']}), {run['runtime_s']} s, {run['result']}.", "",
         "## Layout", "", "- `raw/` -- dftracer traces, one `.pfw.gz` per process", "- `run.json` -- everything below, machine-readable", "- `run.sh` -- exact launch script; `graph_summary.json` -- graph summary"]
    if kind == "verification":
        L.append("- `verification.json` -- the record of running the published package as it is (job, scale, counts vs expected, exit codes, package file hashes)")
    L += ["", "## What was run", "", f"- Input: {run['input']} (scale {run['scale']})", f"- App source commit: {doc['app']['source_commit']}",
          f"- dftracer: {doc['software']['dftracer']}", f"- moirai: {doc['software']['moirai']}"]
    L.append("- Env: " + (", ".join(f"`{k}={v}`" for k, v in sorted(doc["env"].items())) if doc["env"] else "not recorded"))
    L += ["", "## What was captured", "", f"- {info['files']} trace files, {info['bytes']} bytes, {s.get('records', 0)} events",
          f"- {s['entities']} entities, {s['activities']} activities, {s['edges']} edges; {s['dangling']} dangling",
          "- Entity relations: " + (", ".join(f"{k} {v}" for k, v in sorted(rel.items())) or "none"), "- Stores: " + (", ".join(f"{k} {v}" for k, v in sorted(by_store.items())) or "none"),
          "", "## Entity types", "", "| type | role | count | description |", "|---|---|---|---|"]
    for t in sorted(types):
        L.append(f"| {t} | {types[t]['role']} | {by_type.get(t, 0)} | {types[t]['description']} |")
    L.append("")
    return {"README.md": _site("\n".join(L)), "run.json": _site(json.dumps(doc, indent=2)), "graph_summary.json": _site(json.dumps(graph, indent=2))}


def publish_traces_core(root, app, run_dir, ts, kind, summary, meta, run_sh=""):
    """Copy a run's raw traces (+ .dftindex) and write README.md, run.json, run.sh, graph_summary.json into <root>/provenance-traces/<app>/<ts>/ (new dir only).
    run_sh: the exact launch script; default <run_dir>/run.sh, else <run_dir>/../../work/<scale>/run.sh (the package layout)."""
    _names(app, ts)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    root, run_dir = Path(root), Path(run_dir)
    if not root.is_dir():
        raise ValueError(f"publish_root {root} is not a directory")
    raw = run_dir / "raw" if (run_dir / "raw").is_dir() else run_dir
    pf = sorted(raw.glob("*.pfw.gz"))
    if not pf:
        raise ValueError(f"no *.pfw.gz in {raw}")
    rs = next((Path(p) for p in (run_sh, run_dir / "run.sh", run_dir.parent.parent / "work" / run_dir.name / "run.sh") if p and Path(p).is_file()), None)
    if rs is None:
        raise ValueError(f"no run.sh for {run_dir}: the exact launch script is part of a run folder (pass run_sh)")
    rec = next((p for p in (run_dir / "verification.json", run_dir.parent.parent / "verification.json") if p.is_file()), None)
    if kind == "verification" and rec is None:
        raise ValueError("no verification.json beside the run: run repro_verify with in_place and wait for it to finish")
    tgt = root / "provenance-traces" / app / ts
    if not _inside(root, tgt):
        raise ValueError("target is outside publish_root")
    if tgt.exists():
        raise FileExistsError(f"{tgt} exists: refusing to overwrite")
    tmp = tgt.parent / f".tmp-{ts}"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "raw").mkdir(parents=True)
    try:
        for f in pf + ([raw / ".dftindex"] if (raw / ".dftindex").exists() else []):
            subprocess.run(["cp", "-a", str(f), str(tmp / "raw" / f.name)], check=True, capture_output=True)
        subprocess.run(["cp", "-a", str(rs), str(tmp / "run.sh")], check=True, capture_output=True)
        if kind == "verification" and rec is not None:
            subprocess.run(["cp", "-a", str(rec), str(tmp / "verification.json")], check=True, capture_output=True)
        info = {"files": len(pf), "bytes": sum(f.stat().st_size for f in pf)}
        meta = dict(meta)
        if kind == "verification" and rec is not None:
            meta["verification"] = json.loads(rec.read_text())
        for name, text in render_run_files(kind, app, ts, summary, info, meta).items():
            (tmp / name).write_text(text)
        tmp.rename(tgt)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"target": str(tgt), "files": sorted(str(p.relative_to(tgt)) for p in tgt.rglob("*") if p.is_file()), "trace_files": info["files"], "trace_bytes": info["bytes"]}


def perm_check_core(paths, group):
    """Pure (reads only): group = <group>, directories setgid, g=u, no 'other' bits, files without setgid. Returns the list of exceptions."""
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return [f"group {group!r} does not exist here"]
    bad = []
    for top in paths:
        for dp, dn, fn in os.walk(top):
            dn[:] = [x for x in dn if x not in SKIP_DIRS]
            for name in [""] + list(fn):
                p = Path(dp) / name if name else Path(dp)
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    continue
                m, isdir = stat.S_IMODE(st.st_mode), stat.S_ISDIR(st.st_mode)
                if st.st_gid != gid:
                    bad.append(f"{p}: group is not {group}")
                if m & 0o007:
                    bad.append(f"{p}: has 'other' bits {oct(m & 7)}")
                if (m >> 3) & 7 != (m >> 6) & 7:
                    bad.append(f"{p}: group bits {oct((m >> 3) & 7)} differ from user bits {oct((m >> 6) & 7)}")
                if isdir and not m & stat.S_ISGID:
                    bad.append(f"{p}: directory is not setgid")
                if not isdir and m & stat.S_ISGID:
                    bad.append(f"{p}: file has setgid")
    return bad[:60]


def published_dirs(root, app, stamps=None):
    root = Path(root)
    runs = root / "provenance-traces" / app
    if stamps:
        ts = list(stamps)
    else:
        ts = sorted(d.name for d in runs.iterdir() if d.is_dir() and TS_RE.match(d.name) and (d / "run.json").is_file()) if runs.is_dir() else []
    return root / "provenance-repro" / app, [runs / t for t in ts]


def permissions_core(root, script, group, app, stamps=None):
    """Run `<script> --local -q -g <group> <dir>` on provenance-repro/<app> and each run folder only, then verify with perm_check_core."""
    _names(app)
    for t in stamps or []:
        _names(app, t)
    root, script = Path(root), Path(script)
    if not _inside(root, script):
        raise ValueError(f"group_script {script} is outside publish_root: refusing")
    if not (script.is_file() and os.access(script, os.X_OK)):
        raise ValueError(f"group_script {script} is not an executable file")
    pkg, runs = published_dirs(root, app, stamps)
    dirs = [pkg] + runs
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        raise ValueError("not published yet: " + ", ".join(missing))
    for d in dirs:
        if not _inside(root, d) or d.resolve() == root.resolve():
            raise ValueError(f"{d} is not below publish_root: refusing")
    results = []
    for d in dirs:
        r = subprocess.run([str(script), "--local", "-q", "-g", group, str(d)], capture_output=True, text=True, timeout=3600)
        results.append({"dir": str(d), "rc": r.returncode, "tail": (r.stdout + r.stderr)[-300:]})
    bad = perm_check_core(dirs, group)
    return {"ok": not bad and all(x["rc"] == 0 for x in results), "ran": results, "exceptions": bad}


def publish_checks(src_pkg, root, app, group, counts_of):
    """Pure (reads only): [(name, ok, detail, fix)] of the published tree. counts_of(raw_dir) -> {'activities','entities','edges',...} comes from the graph tools."""
    out = []
    root = Path(root)
    pkg, runs = published_dirs(root, app)
    missing = [f for f in PKG_FILES if not (pkg / f).is_file()] + ([] if (pkg / "scripts").is_dir() else ["scripts/"])
    out.append(("published_package", pkg.is_dir() and not missing, f"{pkg}: missing {missing}" if pkg.is_dir() else f"{pkg} does not exist", "repro_publish_package"))
    kinds = {}
    for d in runs:
        try:
            kinds[d.name] = json.loads((d / "run.json").read_text())["run"]["kind"]
        except (OSError, ValueError, KeyError):
            kinds[d.name] = "?"
    out.append(("published_runs", "baseline" in kinds.values() and "verification" in kinds.values(), f"run folders: {kinds}", "repro_publish_traces for kind=baseline and kind=verification"))
    ph = tree_hashes(pkg) if pkg.is_dir() else {}
    if src_pkg and Path(src_pkg).is_dir():
        sh = tree_hashes(src_pkg)
        diff = sorted(k for k in set(sh) | set(ph) if sh.get(k) != ph.get(k))
        out.append(("package_equals_source", not diff, f"differs: {diff[:8]}", "the published copy changed after the copy: remove it only with the user's approval, then repro_publish_package again"))
    if pkg.is_dir():
        found = lint(pkg, Path("/nonexistent-session"))
        out.append(("portable", not found, "; ".join(found)[:380], "repro_build puts values in config.ini; publish the rebuilt package"))
    files = [str(f) for f in sorted((pkg / "scripts").glob("*.sh"))] if pkg.is_dir() else []
    files += [str(d / "run.sh") for d in runs if (d / "run.sh").is_file()]
    parse = [p for p in files if subprocess.run(["bash", "-n", p], capture_output=True).returncode]
    out.append(("scripts_parse", bool(files) and not parse, f"bash -n fails: {parse}" if parse else "no scripts found", "regenerate the package or the run folder"))
    incomplete, count_bad = [], []
    for d in runs:
        need = ["README.md", "run.json", "run.sh", "graph_summary.json"]
        lack = [n for n in need if not (d / n).is_file()] + ([] if list((d / "raw").glob("*.pfw.gz")) else ["raw/*.pfw.gz"])
        if lack:
            incomplete.append(f"{d.name}: {lack}")
            continue
        got = counts_of(str(d / "raw"))
        g = json.loads((d / "run.json").read_text())["captured"]["graph"]
        readme = (d / "README.md").read_text()
        line = f"{got['entities']} entities, {got['activities']} activities, {got['edges']} edges"
        if (g["entities"], g["activities"], g["edges"]) != (got["entities"], got["activities"], got["edges"]) or line not in readme:
            count_bad.append(f"{d.name}: run.json {g}, README has {line in readme}, graph tool {got}")
    out.append(("run_folders_complete", bool(runs) and not incomplete, "; ".join(incomplete), "repro_publish_traces"))
    out.append(("readme_counts", bool(runs) and not count_bad, "; ".join(count_bad)[:380], "regenerate: the README/run.json must come from graph tool counts"))
    vrec, vdetail = False, "no verification folder with verification.json"
    for d in runs:
        if kinds.get(d.name) == "verification" and (d / "verification.json").is_file():
            rec = json.loads((d / "verification.json").read_text())
            same = rec.get("package_hashes") == ph
            vrec = same and rec.get("ok") is True and rec.get("in_place") is True
            vdetail = f"{d.name}: hashes equal={same}, ok={rec.get('ok')}, in_place={rec.get('in_place')}"
    out.append(("verification_record", vrec, vdetail, "repro_verify(in_place=<published package>) again; the package must not change after it"))
    bad = perm_check_core([pkg] + runs, group) if pkg.is_dir() else ["package missing"]
    out.append(("permissions", not bad, "; ".join(bad[:5]), "repro_publish_permissions"))
    return out


FAKE_CHGPERM = r'''#!/bin/bash
# fake chgperm.sh for the sandbox: same interface as the site script (--local -q -g GROUP DIR), same effect, no allocation
grp=moirai; dir=
while [ $# -gt 0 ]; do case "$1" in -g) grp="$2"; shift;; --local|-q|--mpi) ;; -n) shift;; *) dir="$1";; esac; shift; done
[ -d "$dir" ] || { echo "no dir $dir" >&2; exit 2; }
echo "$dir" >> "$(dirname "$0")/chgperm.calls"
chgrp -R "$grp" "$dir" || exit 3
find "$dir" -type d -exec chmod g=u,o-rwx,g+s {} +
find "$dir" -type f -exec chmod g=u,o-rwx,g-s {} +
mkdir -p "$dir/.chgperm_logs"
'''


def _sh(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(0o755)
    return p


def publish_selftest():
    """Sandbox for publishing: a fake publish_root, fake package and traces, a fake chgperm.sh and the current user's own group as the fake group."""
    cases = {}
    fake_group = grp.getgrgid(os.getgid()).gr_name
    with tempfile.TemporaryDirectory(prefix="repro_publish_selftest_") as td:
        td = Path(td).resolve()
        root = td / "vast"
        root.mkdir()
        _sh(root / "chgperm.sh", FAKE_CHGPERM)
        outside = _sh(td / "other_chgperm.sh", FAKE_CHGPERM)
        src = td / "final_provenance"
        _sh(src / "scripts" / LOADER, "#!/bin/bash\nexit 0\n")
        _sh(src / "scripts" / "run_all.sh", '#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n')
        (src / "config.ini").write_text("OUTPUT_ROOT=${HOME}/demo/${APP}\nAPP=demo\n")
        (src / "README.md").write_text("# demo\n")
        (src / "inputs").mkdir()
        (src / "inputs" / "expected.json").write_text("{}")
        gs = str(root / "chgperm.sh")
        pl = plan_core(src, root, "demo", {"baseline": "20260101_010101", "verification": ""}, gs, fake_group)
        cases["plan_ok_writes_nothing"] = pl["status"] == "ok" and not (root / "provenance-repro").exists() and not (root / "provenance-traces").exists() and pl["unknown_timestamps"] == ["verification"]
        cases["plan_has_chgperm_command"] = any("--local -q -g " + fake_group in c and "provenance-repro/demo" in c for c in pl["chgperm_commands"]) and pl["free_bytes"] > 0
        cases["plan_refuses_script_outside_root"] = plan_core(src, root, "demo", {}, str(outside))["status"] == "error"
        cases["plan_refuses_bad_app"] = plan_core(src, root, "../x", {}, gs)["status"] == "error"
        cases["plan_refuses_bad_timestamp"] = plan_core(src, root, "demo", {"baseline": "yesterday"}, gs)["status"] == "error"
        cp = copy_package_core(src, root, "demo")
        cases["package_copied_equal"] = cp["ok"] and cp["files"] == len(tree_hashes(src)) and not cp["hash_mismatch"]
        try:
            copy_package_core(src, root, "demo")
            cases["package_refuses_existing"] = False
        except FileExistsError:
            cases["package_refuses_existing"] = True
        cases["plan_reports_existing"] = plan_core(src, root, "demo", {}, gs)["existing"] == [str(root / "provenance-repro" / "demo")]
        bad_src = td / "bad_pkg"
        _sh(bad_src / "scripts" / "x.sh", "#!/bin/bash\nsource lib_load_config.sh\necho " + os.path.expanduser("~") + "/y\n")
        (bad_src / "config.ini").write_text("A=1\n")
        cases["package_portable_check_catches_home"] = bool(copy_package_core(bad_src, root, "bad")["portable_findings"])

        run_dir = td / "run" / "traces" / "smoke"
        (run_dir / "raw").mkdir(parents=True)
        for n in range(2):
            (run_dir / "raw" / f"p{n}.pfw.gz").write_bytes(b"x" * (10 + n))
        (run_dir / "raw" / ".dftindex").write_text("idx")
        _sh(td / "run" / "work" / "smoke" / "run.sh", "#!/bin/bash\necho run\n")
        summary = {"activities": 7, "entities": 9, "edges": 21, "relations": 2, "types": [{"type": "sequence", "role": "input", "description": "a sequence"}, {"type": "out", "role": "output", "description": "an output"}],
                   "records": 123, "other_events": {}, "dangling": 0, "by_type": {"sequence": 4, "out": 5}, "by_store": {"parallel_fs": 9}, "relation_kinds": {"contains": 2}}
        meta = {"job_id": "fFAKE", "queue": "pdebug", "seconds": 12.5, "result": "COMPLETED", "scale": "baseline", "env": {"MOIRAI_PC_BLOCKS": "16"}}
        r = publish_traces_core(root, "demo", run_dir, "20260101_010101", "baseline", summary, meta)
        tdir = Path(r["target"])
        readme = (tdir / "README.md").read_text()
        rj = json.loads((tdir / "run.json").read_text())
        gj = json.loads((tdir / "graph_summary.json").read_text())
        cases["traces_files_copied"] = sorted(p.name for p in (tdir / "raw").glob("*")) == [".dftindex", "p0.pfw.gz", "p1.pfw.gz"] and (tdir / "run.sh").is_file()
        cases["traces_readme_from_counts"] = "9 entities, 7 activities, 21 edges" in readme and "| sequence | input | 4 | a sequence |" in readme and "fFAKE" in readme and "12.5 s" in readme
        cases["traces_run_json_from_counts"] = rj["captured"]["graph"]["entities"] == 9 and rj["captured"]["trace_files"] == 2 and rj["run"]["kind"] == "baseline" and rj["captured"]["entities_by_store"] == {"parallel_fs": 9}
        cases["traces_graph_summary_keys"] = set(gj) == {"summary", "entity_relations", "types"} and gj["entity_relations"] == {"contains": 2}
        cases["traces_missing_fact_not_invented"] = "not recorded" in readme
        try:
            publish_traces_core(root, "demo", run_dir, "20260101_010101", "baseline", summary, meta)
            cases["traces_refuses_existing"] = False
        except FileExistsError:
            cases["traces_refuses_existing"] = True
        (td / "empty").mkdir(exist_ok=True)
        for label, args in (("bad_timestamp", (run_dir, "../../etc", "baseline")), ("bad_kind", (run_dir, "20260101_010102", "other")),
                            ("no_traces", (td / "empty", "20260101_010103", "baseline")), ("verification_needs_record", (run_dir, "20260101_010104", "verification"))):
            try:
                publish_traces_core(root, "demo", args[0], args[1], args[2], summary, meta)
                cases["traces_refuses_" + label] = False
            except ValueError:
                cases["traces_refuses_" + label] = True
        hashes = tree_hashes(root / "provenance-repro" / "demo")
        (run_dir.parent.parent / "verification.json").write_text(json.dumps({"in_place": True, "ok": True, "package_hashes": hashes, "kind": "verification"}))
        publish_traces_core(root, "demo", run_dir, "20260101_020202", "verification", summary, meta)
        vdir = root / "provenance-traces" / "demo" / "20260101_020202"
        cases["verification_record_copied"] = (vdir / "verification.json").is_file() and "verification.json" in (vdir / "README.md").read_text() and (vdir / "run.sh").is_file()
        shutil.rmtree(root / "provenance-repro" / "bad")

        counts_of = lambda raw: {"activities": 7, "entities": 9, "edges": 21}
        before = {n: ok for n, ok, _, _ in publish_checks(src, root, "demo", fake_group, counts_of)}
        cases["validate_permissions_fail_before_chgperm"] = before["permissions"] is False
        try:
            permissions_core(root, outside, fake_group, "demo")
            cases["permissions_refuses_script_outside_root"] = False
        except ValueError:
            cases["permissions_refuses_script_outside_root"] = True
        try:
            permissions_core(root, gs, fake_group, "../demo")
            cases["permissions_refuses_bad_app"] = False
        except ValueError:
            cases["permissions_refuses_bad_app"] = True
        pr = permissions_core(root, gs, fake_group, "demo")
        calls = (root / "chgperm.calls").read_text().split()
        cases["permissions_applied_and_verified"] = pr["ok"] and not pr["exceptions"]
        cases["permissions_only_published_dirs"] = sorted(calls) == sorted([str(root / "provenance-repro" / "demo"), str(tdir), str(vdir)])
        some = root / "provenance-repro" / "demo" / "README.md"
        some.chmod(0o664 | 0o004)
        cases["permissions_check_catches_other_bits"] = any("other" in x for x in perm_check_core([root / "provenance-repro" / "demo"], fake_group))
        some.chmod(0o660)
        cases["permissions_check_catches_unknown_group"] = bool(perm_check_core([root], "no_such_group_zz"))
        d0 = root / "provenance-repro" / "demo" / "inputs"
        d0.chmod(0o770)
        cases["permissions_check_catches_missing_setgid"] = any("setgid" in x for x in perm_check_core([root / "provenance-repro" / "demo"], fake_group))
        d0.chmod(0o2770)
        after = {n: ok for n, ok, _, _ in publish_checks(src, root, "demo", fake_group, counts_of)}
        cases["validate_all_pass_on_good_tree"] = all(after.values()) and len(after) >= 8
        (root / "provenance-repro" / "demo" / "README.md").write_text("changed\n")
        changed = {n: ok for n, ok, _, _ in publish_checks(src, root, "demo", fake_group, counts_of)}
        cases["validate_catches_changed_package"] = not changed["package_equals_source"] and not changed["verification_record"]
        (tdir / "README.md").write_text("no counts here\n")
        cases["validate_catches_readme_counts"] = not {n: ok for n, ok, _, _ in publish_checks(src, root, "demo", fake_group, counts_of)}["readme_counts"]
        cases["cfg_values_expand"] = cfg_values(src / "config.ini", {"HOME": "/h"})["OUTPUT_ROOT"] == "/h/demo/demo"
        cases["flux_id_found"] = _find_flux_id("x\n[10:00:00] 04_run.sh: batch job f3babTaxk3eF (smoke, 28 min, queue pdebug)\n") == "f3babTaxk3eF"
    bad = [k for k, v in cases.items() if not v]
    line = "repro publish selftest PASS " + " ".join(f"{k}=ok" for k in cases) if not bad else "repro publish selftest FAIL " + ",".join(bad)
    return {"status": "ok" if not bad else "error", "line": line, "cases": cases}


def _verification_record(pipeline):
    """Write <OUTPUT_ROOT>/verification.json once the in-place job has finished. Returns {status, ...}; status 'running' while it runs."""
    c = P.load(pipeline)
    o = P.stage(c, STAGE)["outputs"]
    vf = o.get("verify") or {}
    if not vf.get("in_place"):
        return {"status": "error", "error": "no in-place verification was started: repro_verify(in_place=...)"}
    j = J.get(pipeline, vf["job"], 40000)
    if j["status"] == "running":
        return {"status": "running"}
    out, pkg = Path(vf["output_root"]), Path(vf["in_place"])
    run = out / "run.json"
    rj = json.loads(run.read_text()) if run.exists() else {}
    flux = _find_flux_id(j.get("log_tail", ""))
    t_run = _flux_field(flux, "{t_run}") if flux else ""
    secs = _flux_field(flux, "{runtime}") if flux else ""
    rsh = out / "work" / vf["scale"] / "run.sh"
    try:
        start = float(t_run)
        src = "flux jobs {t_run}"
    except ValueError:
        start = rsh.stat().st_mtime if rsh.exists() else time.time()
        src = "mtime of run.sh (flux unavailable)"
    now = tree_hashes(pkg)
    rec = {"kind": "verification", "in_place": True, "package": "provenance-repro", "scale": vf["scale"], "job": vf["job"], "flux_job": flux, "job_start": _stamp(start), "job_start_source": src,
           "seconds": float(secs) if secs else j.get("elapsed"), "exit_codes": {"run_all": j["rc"]}, "counts": rj.get("counts"), "expected": rj.get("expected"), "failures": rj.get("failures"),
           "ok": bool(rj.get("ok")) and j["rc"] == 0, "overrides": vf.get("overrides", {}), "not_exercised": vf.get("not_exercised", []), "package_hashes_before": vf.get("hashes_before"),
           "package_hashes": now, "package_unchanged": now == vf.get("hashes_before"), "login_shell": "env -i bash -l"}
    rec["ok"] = rec["ok"] and rec["package_unchanged"]
    (out / "verification.json").write_text(json.dumps(rec, indent=2))
    P.record(pipeline, STAGE, outputs={"verify": {**vf, "record": str(out / "verification.json"), "job_start": rec["job_start"], "flux_job": flux, "ok": rec["ok"]}}, note="in-place verification record written")
    return {"status": "ok", "record": str(out / "verification.json"), **{k: rec[k] for k in ("job_start", "ok", "counts", "expected", "package_unchanged", "flux_job")}}


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
    def repro_verify(pipeline: str, isolation_root: str = "", scale: str = "smoke", reuse_venv: str = "", limit: int = 3600, wait: int = 300, moirai_venv: str = "", in_place: str = "") -> dict:
        """Prove the package works: run it in a clean login shell, with nothing inherited (env -i, bash -l). Use after repro_build (copy mode) or after repro_publish_package (in_place).
        Copy mode: isolation_root, a parent folder outside the session on a file system compute nodes can see; the package is copied to a new folder there. In-place mode (in_place = the
        published package folder, e.g. <publish_root>/provenance-repro/<app>): the package is run AS IT IS, no copy; only HOME, USER, TERM and SCALE are passed, plus the venv/limit overrides
        you give (they are written into the record). Its trace output goes to the package's own OUTPUT_ROOT (config.ini). When the job has finished a verification record
        <OUTPUT_ROOT>/verification.json is written (job ids, scale, seconds, counts vs expected.json, exit codes, the package's file hashes before and after, whether unchanged); call again
        with the same in_place after a timeout to complete the record. scale: smoke or baseline. reuse_venv: a venv to reuse instead of building one (batch mode: the app's own venv).
        moirai_venv: batch mode only, the moirai venv to reuse (needed with reuse_venv). limit: seconds for the whole run; in batch mode the flux batch time limit is derived from it.
        wait: seconds to wait now; the run goes on in the background (a batch run takes ~8 min) and repro_validate judges it later. Batch mode never starts a service on the login node.
        Returns {status, job, iso_dir or in_place, mode, not_exercised, log_tail, record (in_place)}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if scale not in ("smoke", "baseline"):
            return {"status": "error", "error": "scale is smoke or baseline"}
        if in_place:
            pk = Path(in_place).resolve()
            prev = o.get("verify") or {}
            if prev.get("in_place") == str(pk) and prev.get("scale") == scale and not prev.get("record"):
                r = J.wait(pipeline, prev["job"], wait)
                return {"status": r["status"], "job": prev["job"], "in_place": str(pk), "record": _verification_record(pipeline)}
            if not (pk / "config.ini").is_file() or not (pk / "scripts" / "run_all.sh").is_file():
                return {"status": "error", "error": f"{pk} is not a package folder (config.ini, scripts/run_all.sh)"}
            if str(pk).startswith(str(P.work(c).parent.resolve()) + os.sep):
                return {"status": "error", "error": "in_place must be the published folder, outside the session workspace"}
            batch = bool(re.search(r"^MODE=batch\s*$", (pk / "config.ini").read_text(), re.M))
            if batch and bool(reuse_venv) != bool(moirai_venv):
                return {"status": "error", "error": "batch mode reuses both venvs or neither: give reuse_venv (the app venv) and moirai_venv together"}
            env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
            env.update(TERM="dumb", SCALE=scale)
            overrides, not_exercised = {}, []
            if reuse_venv:
                overrides.update(REUSE_VENV="1", VENV=reuse_venv)
                not_exercised.append("app environment build and moirai install" if batch else "environment build")
            if batch:
                if moirai_venv:
                    overrides["MOIRAI_VENV"] = moirai_venv
                overrides[f"LIMIT_MIN_{scale}"] = str(max(1, int(limit) // 60 - 2))
            env.update(overrides)
            out_root = cfg_values(pk / "config.ini", dict(env)).get("OUTPUT_ROOT", "")
            if not out_root or "${" in out_root:
                return {"status": "error", "error": f"cannot resolve OUTPUT_ROOT from config.ini: {out_root!r}"}
            (Path(out_root) / "verification.json").unlink(missing_ok=True)
            hashes = tree_hashes(pk)
            cmd = f"cd {shlex.quote(str(pk))} && bash scripts/run_all.sh"
            args = " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
            j = J.start(pipeline, STAGE, "in-place-verify", f"env -i {args} bash -lc {shlex.quote(cmd)}\n", str(pk.parent), limit=limit)
            P.record(pipeline, STAGE, outputs={"verify": {"job": j["id"], "in_place": str(pk), "output_root": out_root, "scale": scale, "mode": "batch" if batch else "generic",
                                                           "overrides": overrides, "not_exercised": not_exercised, "hashes_before": hashes}}, note=f"in-place run of {scale} from {pk}")
            r = J.wait(pipeline, j["id"], wait)
            return {"status": r["status"], "job": j["id"], "rc": r["rc"], "in_place": str(pk), "output_root": out_root, "mode": "batch" if batch else "generic", "not_exercised": not_exercised,
                    "log_tail": r.get("log_tail", "")[-2500:], "record": _verification_record(pipeline)}
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        if not isolation_root:
            return {"status": "error", "error": "give isolation_root (copy mode) or in_place (the published package)"}
        batch = o.get("mode") == "batch"
        if batch and bool(reuse_venv) != bool(moirai_venv):
            return {"status": "error", "error": "batch mode reuses both venvs or neither: give reuse_venv (the app venv) and moirai_venv together"}
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
        original counts. When the package was published (repro_publish_package recorded publish_root) the published tree is checked too, as repro_publish_validate does (group moirai).
        Use before the last gate. Returns the verdict {status, failed, unknown, passed, total}."""
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
        run = (Path(vf["output_root"]) if vf.get("in_place") else Path(vf["iso_dir"]) / "out") / "run.json"
        if v.check("reproduced", run.exists() and json.loads(run.read_text()).get("ok"), run.read_text()[:300] if run.exists() else "no run.json", "the counts differ: is the app deterministic? Is the input the same?"):
            note = f"; NOT exercised: {', '.join(vf['not_exercised'])}" if vf.get("not_exercised") else ""
            P.record(pipeline, STAGE, outputs={"summary": f"reproduced in a clean shell ({vf.get('scale')}): {json.loads(run.read_text())['counts']}{note}"})
        if o.get("publish_root"):
            for name, ok, detail, fix in publish_checks(o["package"], o["publish_root"], o.get("publish_app", ""), "moirai", lambda d: T.read(d).counts()):
                v.check("published_" + name, ok, detail, fix)
        return P.finish_validation(pipeline, v)

    @mcp.tool()
    def repro_batch_selftest() -> dict:
        """Check the batch mode on pure renders, with no cluster: the per-chunk wrapper template, the 04_run text (both scales), the chunk-failure patterns on the real failing run.out text and a clean
        one, the command parse and config render, and that no script holds a user, workspace, session or job path. Use after changing repro_package. Returns {status, line, cases, notes}."""
        return _selftest()

    @mcp.tool()
    def repro_publish_selftest() -> dict:
        """Sandbox test of publishing, nothing on the cluster and no real chgperm.sh: a temp publish_root, a fake package and fake traces, a fake chgperm.sh and the caller's own group as the
        fake group. Covers the dry-run plan (writes nothing; refuses a script outside the root, a bad app name or timestamp), the package copy (hash equal, refuses an existing target, catches a
        home path), the traces copy and the README / run.json / graph_summary.json made from fake counts (refuses an existing target, bad timestamp, bad kind, no traces, a verification without
        its record), permission application (only the published folders; refuses a script outside the root) and verification (other bits, wrong group, missing setgid), and the validator checks
        (changed package, wrong README counts). Use after any change to the publishing code. Returns {status, line, cases}: line is 'repro publish selftest PASS ...' or 'FAIL <cases>'."""
        return publish_selftest()

    @mcp.tool()
    def repro_publish_plan(pipeline: str, publish_root: str, app: str = "elmerfold", group_script: str = "", group: str = "moirai", baseline_flux_job: str = "", baseline_timestamp: str = "") -> dict:
        """DRY RUN of publishing: starts nothing and writes nothing. Use after repro_build, before repro_publish_package. publish_root: the shared root (it must exist; it holds provenance-repro/
        and provenance-traces/). app: the folder name. group_script: the site's chgperm.sh (must be inside publish_root; default <publish_root>/chgperm.sh). group: its -g value. baseline_flux_job:
        the flux job id of the baseline batch (default: the id in the baseline stage's job log); baseline_timestamp: YYYYMMDD_HHMMSS override of its job start (default `flux jobs -o {t_run}`
        of that job). The verification run's job start is known only after repro_verify(in_place) ran (taken from the pipeline record then). Returns {status, problems, targets, runs (kind,
        timestamp, target, gets), existing (nothing is overwritten), package_bytes, free_bytes, chgperm_commands, unknown_timestamps, baseline_source}; status error when anything exists or is outside publish_root."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        base = P.stage(c, "baseline")["outputs"]
        src = ""
        ts = baseline_timestamp
        if not ts:
            fid = baseline_flux_job
            if not fid and base.get("job"):
                fid = _find_flux_id(J.get(pipeline, base["job"], 40000).get("log_tail", ""))
                src = "flux id from the baseline job log"
            t = _flux_field(fid, "{t_run}") if fid else ""
            try:
                ts = _stamp(float(t))
                src = f"flux jobs -o {{t_run}} {fid} ({src or 'given'})"
            except ValueError:
                ts = ""
                src = f"unknown: flux id {fid or 'not found'}; pass baseline_flux_job or baseline_timestamp"
        vf = o.get("verify") or {}
        vts = vf.get("job_start", "") if vf.get("in_place") else ""
        r = plan_core(o["package"], publish_root, app, {"baseline": ts, "verification": vts}, group_script or str(Path(publish_root) / "chgperm.sh"), group)
        r["baseline_source"] = src or "given"
        if not ts and r["status"] != "error":
            r["status"] = "needs_input"
        return r

    @mcp.tool()
    def repro_publish_package(pipeline: str, publish_root: str, app: str = "elmerfold") -> dict:
        """Copy final_provenance to <publish_root>/provenance-repro/<app>/ with cp -a (new dir only: refuses an existing target), then compare every file hash with the source and run the
        portable check (no user, home, session path, job id; scripts parse and source the loader) on the copy. Use after repro_publish_plan said ok. Writes under publish_root only. Records
        publish_root and app in the stage outputs. Returns {status, target, files, hash_mismatch, portable_findings}; status error when anything differs or is found."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        try:
            r = copy_package_core(o["package"], publish_root, app, P.work(c).parent.resolve())
        except (ValueError, FileExistsError, OSError) as e:
            return {"status": "error", "error": str(e)}
        P.record(pipeline, STAGE, outputs={"publish_root": str(Path(publish_root).resolve()), "publish_app": app, "published_package": r["target"]}, note=f"package published to {r['target']}")
        return {"status": "ok" if r["ok"] else "error", **r}

    @mcp.tool()
    def repro_publish_traces(pipeline: str, publish_root: str, app: str, run_dir: str = "", timestamp: str = "", kind: str = "baseline", flux_job: str = "") -> dict:
        """Copy a run's raw traces (+ .dftindex) and write README.md, run.json, run.sh, graph_summary.json into <publish_root>/provenance-traces/<app>/<timestamp>/ (new dir only). Every
        count comes from the graph tools (moirai trace reader), never typed; a missing fact reads 'not recorded'. kind=baseline: the pipeline's baseline run (run_dir default: the folder
        holding the baseline stage's raw traces; run.sh: the baseline stage's run script; timestamp default: its flux job start, as repro_publish_plan). kind=verification: the in-place run of the
        published package (run_dir default: <OUTPUT_ROOT>/traces/<scale>; run.sh from <OUTPUT_ROOT>/work/<scale>; timestamp default: the job start in the verification record, which is written
        first if the job has finished; verification.json is copied along). flux_job: the flux id of the run, for job id / runtime / start. Returns {status, target, files, trace_files, trace_bytes, counts}."""
        c = P.load(pipeline)
        meta, run_sh, wrapper = {}, "", None
        try:
            if kind == "baseline":
                base = P.stage(c, "baseline")["outputs"]
                rd = Path(run_dir) if run_dir else Path(base["traces"]).parent
                fid = flux_job or (_find_flux_id(J.get(pipeline, base["job"], 40000).get("log_tail", "")) if base.get("job") else "")
                meta.update(queue=base.get("queue"), scale="baseline")
                run_sh = base.get("run_script") or base.get("batch_script") or ""
                wrapper = base.get("wrapper")
            elif kind == "verification":
                st = _verification_record(pipeline)
                if st["status"] != "ok":
                    return {"status": "error", "error": f"the in-place verification is not finished or not started: {st}"}
                vf = P.stage(P.load(pipeline), STAGE)["outputs"]["verify"]
                rd = Path(run_dir) if run_dir else Path(vf["output_root"]) / "traces" / vf["scale"]
                fid = flux_job or vf.get("flux_job", "")
                timestamp = timestamp or vf.get("job_start", "")
                meta.update(scale=vf["scale"], queue=cfg_values(Path(vf["in_place"]) / "config.ini", {}).get("QUEUE"))
                wrapper = str(Path(vf["output_root"]) / "submit_moirai.sh")
            else:
                return {"status": "error", "error": f"kind must be one of {KINDS}"}
            if fid:
                meta["job_id"] = fid
                meta["seconds"] = _flux_field(fid, "{runtime}") or None
                meta["result"] = _flux_field(fid, "{result}") or None
                t = _flux_field(fid, "{t_run}")
                if t:
                    meta["start_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(t)))
                    timestamp = timestamp or _stamp(float(t))
            if not timestamp:
                return {"status": "error", "error": "no timestamp: pass timestamp or flux_job (the job start)"}
            pkgcfg = Path(publish_root) / "provenance-repro" / app / "config.ini"
            if pkgcfg.is_file():
                cv = cfg_values(pkgcfg, {})
                meta.update(app_commit=cv.get("APP_REF"), dftracer=cv.get("DFTRACER_SPEC"), moirai=cv.get("MOIRAI_SPEC"), input=cv.get("INCHUNKS") if kind == "baseline" else None)
            if wrapper and Path(wrapper).is_file():
                meta["env"] = {m.group(1): _site(m.group(2).strip('"')) for m in re.finditer(r"^export ((?:MOIRAI|DFTRACER)_\w+)=(\S+)", Path(wrapper).read_text(), re.M)}
            raw = rd / "raw" if (rd / "raw").is_dir() else rd
            g = T.read(str(raw))
            summary = g.summary()
            r = publish_traces_core(publish_root, app, rd, timestamp, kind, summary, meta, run_sh)
        except (ValueError, FileExistsError, OSError, KeyError, subprocess.CalledProcessError) as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}
        P.record(pipeline, STAGE, outputs={f"published_{kind}": r["target"]}, note=f"{kind} traces published to {r['target']}")
        return {"status": "ok", **r, "counts": g.counts()}

    @mcp.tool()
    def repro_publish_permissions(publish_root: str, group_script: str, group: str = "moirai", app: str = "elmerfold", timestamps: str = "") -> dict:
        """Set the group on the PUBLISHED trees only: runs `<group_script> --local -q -g <group> <dir>` on provenance-repro/<app> and on each provenance-traces/<app>/<ts> (timestamps: a comma
        list; default every run folder holding a run.json), then verifies: group = <group>, directories setgid, group bits equal user bits, no 'other' bits, files without setgid; every exception
        is returned. Refuses a group_script outside publish_root or a folder outside it. Run only after publish_package and publish_traces. Use once, after the user agreed; it changes
        permissions under publish_root. Returns {status, ok, ran (dir, rc), exceptions}."""
        try:
            r = permissions_core(publish_root, group_script, group, app, [t.strip() for t in timestamps.split(",") if t.strip()] or None)
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}
        return {"status": "ok" if r["ok"] else "error", **r}

    @mcp.tool()
    def repro_publish_validate(pipeline: str, publish_root: str, app: str = "elmerfold", group: str = "moirai") -> dict:
        """Validator of the published tree: the package and run folders are complete; the package copy equals final_provenance; the package is portable and its scripts (and each run.sh) parse
        (bash -n); each run folder's README.md and run.json counts equal the graph tool's counts of its raw traces; a verification record exists, ran in place, passed, and its package hashes equal
        the published files; permissions are as set by chgperm (group, setgid, g=u, no other). Use after repro_publish_permissions. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        v = P.Verdict(STAGE)
        for name, ok, detail, fix in publish_checks(o.get("package"), publish_root, app, group, lambda d: T.read(d).counts()):
            v.check(name, ok, detail, fix)
        return P.finish_validation(pipeline, v)
