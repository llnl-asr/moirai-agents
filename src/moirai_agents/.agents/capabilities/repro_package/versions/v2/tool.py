"""Last stage of the run pipeline: final_provenance/, a folder that reproduces the run for someone who has neither this session nor this account, and the proof that it does.

Same shape as dftracer-agents' reproduction packages: config.ini is the only file with site values, every script sources lib_load_config.sh, the instrumentation is a patch
against a pinned app, and a standalone checker judges the result. It is verified by running it from a fresh copy in a clean login shell.
Two modes: generic (one command under moirai-service in the running shell) and batch (a generated flux batch run.sh; the per-chunk wrapper starts the service on the compute node).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
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
STANDALONE = {"check_batch.sh"}
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

# ------------------------------------------------------------------ batch mode texts
WRAPPER_TMPL = r'''#!/bin/bash
# moirai reproduction package: per-chunk submit wrapper. flux bulksubmit runs it on the exclusive worker node as: submit_moirai.sh <chunk csv>
# It starts moirai-service in the background of THIS shell (never via flux run/submit), runs the original submit script as a child,
# and stops the service with the same environment. Arguments are passed to the original unchanged.
ORIG=@ORIG@
ENVF=@ENVF@
[ -f "${ORIG}" ] || { echo "moirai-wrapper: original submit script ${ORIG} not found" >&2; exit 2; }
[ -f "${ENVF}" ] || { echo "moirai-wrapper: environment file ${ENVF} not found" >&2; exit 2; }
source "${ENVF}" || { echo "moirai-wrapper: sourcing ${ENVF} failed" >&2; exit 2; }
command -v moirai-service >/dev/null 2>&1 || { echo "moirai-wrapper: moirai-service not on PATH after sourcing ${ENVF}" >&2; exit 2; }
_job="${FLUX_JOB_ID:-$$}"
_host="$(hostname)"
export MOIRAI_LOG_FILE=@STAGE@/raw/prov
export MOIRAI_DATA_DIR=@DATA@
export MOIRAI_SERVICE_SHM="@SHM@-${_job}"
export MOIRAI_PC_BLOCKS=${MOIRAI_PC_BLOCKS:-16}
export MOIRAI_PC_FLUSH_MS=${MOIRAI_PC_FLUSH_MS:-250}
# an explicit MOIRAI_DFTRACER_SERVICE wins; otherwise the one installed in the moirai venv
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  for _c in @MOIRAI_VENV@/lib/python*/site-packages/dftracer/bin/dftracer_service; do
    if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
  done
  [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service found in @MOIRAI_VENV@" >&2; exit 2; }
fi
_svclog=@STAGE@/service-${_host}-${_job}.log
echo "${_host}" >> @STAGE@/hosts.txt
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
CHECK_BATCH = r'''#!/bin/bash
# Standalone. A flux batch job's return code does not reflect its bulksubmit chunk jobs: judge the batch --output file and hosts.txt.
#   check_batch.sh <batch --output file, or "" when run.sh has none> <hosts.txt> [batch job id]
# exit 0 clean; 4 a chunk job failed; 5 no host in hosts.txt (no wrapper ran, nothing traced); 6 the --output file is not readable.
set -uo pipefail
_out="${1:-}"
_hosts="${2:-}"
jid="${3:-batch}"
if [ -n "${_out}" ]; then
  if [ ! -r "${_out}" ]; then
    echo "check_batch: batch job ${jid} ended but its --output file ${_out} is not readable: cannot tell whether the chunk jobs ran" >&2
    exit 6
  fi
  _failed="$(grep -E ' failed[[:space:]]*$' "${_out}" | head -5)"
  _codes="$(grep -E 'exited with exit code [1-9][0-9]*' "${_out}" | head -5)"
  if [ -n "${_failed}" ] || [ -n "${_codes}" ]; then
    _ids="$(printf '%s\n' "${_failed}" | sed -n 's/^.*: \([^ ][^ ]*\) failed[[:space:]]*$/\1/p' | tr '\n' ' ')"
    echo "check_batch: batch job ${jid} returned 0 but a chunk job FAILED (chunk job id(s): ${_ids:-unknown}); run.sh ends normally even when its bulksubmit chunk job fails. Evidence in ${_out}:" >&2
    printf '%s\n%s\n' "${_codes}" "${_failed}" >&2
    exit 4
  fi
fi
if [ ! -s "${_hosts}" ]; then
  echo "check_batch: batch job ${jid} left no host in ${_hosts}: no per-chunk wrapper ran, so no service was started and nothing was traced" >&2
  exit 5
fi
echo "check_batch: batch job ${jid} clean: no failed chunk job in ${_out:-<no --output header>}; hosts: $(sort -u "${_hosts}" | tr '\n' ' ')"
exit 0
'''
BATCH_STEPS = {
    "00_check_prereqs.sh": r'''# Commands, and that the runtime this package needs can be reached.
for c in git patch python3 flux; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 1; }; done
[[ -x "$APP_VENV/bin/python" ]] || { echo "APP_VENV $APP_VENV has no bin/python: the application environment must exist (README, Application environment)" >&2; exit 1; }
[[ "$REUSE_VENV" == 1 ]] && { "$PY" -c "import moirai" || { echo "REUSE_VENV=1 but $PY has no moirai" >&2; exit 1; }; }
log "ok"
''',
    "01_get_source.sh": STEPS["01_get_source.sh"],
    "02_setup_env.sh": r'''# The moirai venv at the pinned versions (REUSE_VENV=1 uses an existing one). The application venv is NOT built here.
if [[ "$REUSE_VENV" == 1 ]]; then
  log "NOT EXERCISED: the moirai venv build; reusing $MOIRAI_VENV"
else
  python3 -m venv "$MOIRAI_VENV"
  "$PY" -m pip install -q --upgrade pip scikit-build-core pybind11
  DFTRACER_WHEEL=1 "$PY" -m pip install -q "$DFTRACER_SPEC"
  "$PY" -m pip install -q --no-build-isolation "$MOIRAI_SPEC"
  [[ -z "$UTILS_SPEC" ]] || "$PY" -m pip install -q "$UTILS_SPEC"
fi
log "NOT EXERCISED: the application environment build (setup-env.sh, see README); using the existing $APP_VENV"
[[ -x "$APP_VENV/bin/python" ]] || { echo "APP_VENV $APP_VENV has no bin/python: build the application environment first (README, Application environment)" >&2; exit 1; }
''',
    "03_build.sh": r'''# No compilation: the app runs under its own venv. Write the environment file, the symlink shim and the per-scale wrappers.
require MOIRAI_VENV APP_VENV APP_SUBDIR SUBMIT_REL
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
SHIM="$OUTPUT_ROOT/moirai_shim"
ENVF="$OUTPUT_ROOT/moirai_env.sh"
rm -rf "$SHIM"; mkdir -p "$SHIM"
for n in moirai dftracer dftracer.libs; do
  if [[ -e "$SITE/$n" ]]; then ln -s "$SITE/$n" "$SHIM/$n"; fi
done
[[ -e "$SHIM/moirai" && -e "$SHIM/dftracer" ]] || { echo "moirai or dftracer not found in $SITE: is $MOIRAI_VENV the moirai venv?" >&2; exit 1; }
{
  echo "# Generated by 03_build.sh: the environment of the patched app. The app keeps its own venv; moirai comes through the shim."
  echo "export MOIRAI_SERVICE=$MOIRAI_VENV/bin/moirai-service"
  echo "export PATH=\"\$PATH\":$MOIRAI_VENV/bin"
  echo "export PYTHONPATH=$SHIM:$SRC\"\${PYTHONPATH:+:\$PYTHONPATH}\""
  echo "export LD_LIBRARY_PATH=\"\${LD_LIBRARY_PATH:+\$LD_LIBRARY_PATH:}\"$SITE/moirai/lib64:$SITE/dftracer/lib64:$SITE/dftracer/lib"
} > "$ENVF"
tmpl="$(<"$PKG_DIR/templates/submit_moirai.sh.in")"
for s in smoke baseline; do
  sd="$OUTPUT_ROOT/traces/$s"
  mkdir -p "$sd/raw"
  t="$tmpl"
  t="${t//@ORIG@/$SRC/$SUBMIT_REL}"
  t="${t//@ENVF@/$ENVF}"
  t="${t//@STAGE@/$sd}"
  t="${t//@DATA@/$SRC}"
  t="${t//@SHM@//moirai-repro}"
  t="${t//@MOIRAI_VENV@/$MOIRAI_VENV}"
  printf '%s\n' "$t" > "$sd/submit_moirai.sh"
  chmod 755 "$sd/submit_moirai.sh"
done
log "environment $ENVF, shim $SHIM, wrappers in $OUTPUT_ROOT/traces/{smoke,baseline}"
''',
    "04_run.sh": r'''# The run: make the input, generate run.sh, submit ONE flux batch, wait, judge it.
# No service starts here: each app job runs the wrapper, which starts its own service on the compute node.
require APP_VENV CHUNK_DIR GEN_CONFIG APP_SUBDIR QUEUE MODULES SMOKE_LINES SMOKE_MINUTES BASELINE_MINUTES
set +u
type module >/dev/null 2>&1 || { echo "the 'module' command is not defined in this shell: use a login shell" >&2; exit 1; }
module load $MODULES || { echo "module load $MODULES failed" >&2; exit 1; }
set -u
command -v dbcast >/dev/null 2>&1 || { echo "dbcast not on PATH after module load $MODULES: flux batch would propagate a shell without it" >&2; exit 1; }
command -v flux >/dev/null 2>&1 || { echo "missing command: flux" >&2; exit 1; }
STAGE_DIR="$OUTPUT_ROOT/traces/$SCALE"
[[ -x "$STAGE_DIR/submit_moirai.sh" ]] || { echo "no wrapper in $STAGE_DIR: run 03_build.sh first" >&2; exit 1; }
case "$SCALE" in
  smoke)
    MINUTES="$SMOKE_MINUTES"
    INCHUNKS="$OUTPUT_ROOT/inputs/smoke_chunks"
    first="$(find "$CHUNK_DIR" -maxdepth 1 -type f | sort | sed -n 1p)"
    [[ -n "$first" ]] || { echo "no chunk file in $CHUNK_DIR" >&2; exit 1; }
    rm -rf "$INCHUNKS"; mkdir -p "$INCHUNKS"
    head -n "$SMOKE_LINES" "$first" > "$INCHUNKS/$(basename "$first")"
    log "smoke input: first $SMOKE_LINES lines of $first in $INCHUNKS"
    ;;
  baseline) MINUTES="$BASELINE_MINUTES"; INCHUNKS="$CHUNK_DIR" ;;
  *) echo "SCALE must be smoke or baseline, not $SCALE" >&2; exit 2 ;;
esac
RUN_SH="$STAGE_DIR/run.sh"
rm -rf "$STAGE_DIR/raw" "$RUN_SH" "$STAGE_DIR/hosts.txt"
rm -f "$STAGE_DIR"/service-*.log
mkdir -p "$STAGE_DIR/raw"
(cd "$SRC" && "$APP_VENV/bin/python" utils/gen_launch_script.py --config "$GEN_CONFIG" --inchunks "$INCHUNKS" \
  --submit-script "$STAGE_DIR/submit_moirai.sh" --src-dir "$SRC" --output-dir "$STAGE_DIR/output" --env-dir "$APP_VENV" \
  --timing-dir "$STAGE_DIR/timing" --log-dir "$STAGE_DIR/logs" --prestage-script "$SRC/$APP_SUBDIR/prestage_check.sh" \
  --overwrite --cwd "$SRC/$APP_SUBDIR" -- "$RUN_SH")
[[ -s "$RUN_SH" ]] || { echo "gen_launch_script.py wrote no $RUN_SH" >&2; exit 1; }
grep -qF "$STAGE_DIR/submit_moirai.sh" "$RUN_SH" || { echo "run.sh does not run the moirai wrapper: the traced app would run without a service" >&2; exit 1; }
grep -qF "$SRC" "$RUN_SH" || { echo "run.sh does not point into the patched tree $SRC" >&2; exit 1; }
OUT="$(sed -n 's/^#flux:[[:space:]]*--output[= ][= ]*\([^[:space:]]*\).*$/\1/p' "$RUN_SH" | sed -n 1p)"
OUT="$(printf %s "$OUT" | tr -d "'\"")"
[[ -z "$OUT" || "$OUT" == /* ]] || OUT="$STAGE_DIR/$OUT"
[[ -z "$OUT" ]] || rm -f "$OUT"
jid="$(cd "$STAGE_DIR" && flux batch -t "$MINUTES" --queue "$QUEUE" "$RUN_SH")" || { echo "flux batch submission failed" >&2; exit 1; }
log "batch job: $jid (run.sh $RUN_SH, output ${OUT:-none})"
cleanup() { trap - TERM INT; flux cancel "$jid" 2>/dev/null; exit 143; }
trap cleanup TERM INT
flux watch "$jid" || true
res="$(flux jobs -n -o '{result}' "$jid" 2>/dev/null | tr -d ' ' || true)"
rc="$(flux jobs -n -o '{returncode}' "$jid" 2>/dev/null | tr -d ' ' || true)"
log "batch job $jid result=$res returncode=$rc"
if [[ "$res" != COMPLETED || "$rc" != 0 ]]; then
  echo "batch job $jid FAILED (result=$res returncode=$rc): read ${OUT:-its output}, the chunk logs in $STAGE_DIR/logs and $STAGE_DIR/service-*.log" >&2
  exit 1
fi
bash "$PKG_DIR/scripts/check_batch.sh" "$OUT" "$STAGE_DIR/hosts.txt" "$jid"
log "traces in $TRACES"
''',
    "05_validate.sh": STEPS["05_validate.sh"],
}


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


def batch_config(app, output_root, app_venv, moirai_venv, url, ref, pins, chunk_dir, gen_config="configs/tuolumne_test.yaml", queue="pdebug",
                 modules="mpifileutils/0.11.1", app_subdir="monomer-structure-prediction", submit_rel="", smoke_lines=2, smoke_minutes=30, baseline_minutes=45):
    """Pure: the text of config.ini for batch mode. Values are written as given: the caller has already made them nobody's (${HOME}, ${USER})."""
    pins = pins or {}
    cfg = ["# The ONLY file with site or user values. Every script sources scripts/lib_load_config.sh, which reads it. The environment overrides it.", "", "[paths]",
           f"APP={app}", f"OUTPUT_ROOT={output_root}", "MODE=batch", f"MOIRAI_VENV={moirai_venv}", "# The application environment: must exist (see README, Application environment). It is not built here.",
           f"APP_VENV={app_venv}", "VENV=${MOIRAI_VENV}", "# 1 uses an existing moirai venv (MOIRAI_VENV).", "REUSE_VENV=0", "",
           "[repos]", f"APP_URL={url}", f"APP_REF={ref}", f"DFTRACER_SPEC={pins.get('dftracer', '')}", f"MOIRAI_SPEC={pins.get('moirai', '')}", f"UTILS_SPEC={pins.get('dftracer-utils', '')}", "",
           "[batch]", "# The batch input: the smoke chunk dir is made from the first SMOKE_LINES lines of the first file of CHUNK_DIR.", f"CHUNK_DIR={chunk_dir}", f"GEN_CONFIG={gen_config}",
           f"APP_SUBDIR={app_subdir}", f"SUBMIT_REL={submit_rel or app_subdir + '/submit_smsp.sh'}", f"QUEUE={queue}", f"MODULES={modules}", f"SMOKE_LINES={smoke_lines}",
           f"SMOKE_MINUTES={smoke_minutes}", f"BASELINE_MINUTES={baseline_minutes}", "", "[run]", "# smoke or baseline.", "SCALE=smoke", ""]
    return "\n".join(cfg)


def batch_readme(name, counts, artifacts):
    return f"""# {name}: provenance reproduction package (batch mode)

Reproduces the moirai provenance of {name}: {counts['activities']} activities and {counts['entities']} entities in the original run.
Scientific artifacts: {', '.join(artifacts)}.

The application is launched by a generated `flux batch` run.sh (made by the app's `utils/gen_launch_script.py`). Its app job runs a per-chunk wrapper
(`submit_moirai.sh`, from `templates/submit_moirai.sh.in`) that starts `moirai-service` on the compute node, runs the app's own submit script and stops the service.
No service ever runs on the login node.

## Use

```bash
$EDITOR config.ini                 # the only file with site values
bash scripts/run_all.sh            # SCALE=smoke (2 sequences) by default; SCALE=baseline for the full split
```

Run it from a login shell on the cluster (`bash -l`): `module` must exist and `mpifileutils/0.11.1` (for `dbcast`) is loaded by `04_run.sh`.
The result is judged by `scripts/prov_check.py` against `inputs/expected.json` and written to `$OUTPUT_ROOT/run.json`.

| Script | Does |
|---|---|
| `00_check_prereqs.sh` | commands, and that `APP_VENV` has a python |
| `01_get_source.sh` | the app at its pinned 40-character `APP_REF`, plus `patches/provenance.patch` |
| `02_setup_env.sh` | the moirai venv at the pinned versions (`REUSE_VENV=1` skips it); NOT the application environment |
| `03_build.sh` | `moirai_env.sh`, the symlink shim (moirai, dftracer in the app's `PYTHONPATH`; nothing is installed in the app venv) and both wrappers |
| `04_run.sh` | the smoke input, `run.sh`, one `flux batch`, `flux watch`, then `check_batch.sh` |
| `05_validate.sh` | the provenance check against `inputs/expected.json` |
| `check_batch.sh` | a batch return code does not reflect its chunk jobs: fails on `exited with exit code <n>` or a line ending ` failed` in the batch output, and on an empty hosts.txt |

## Application environment (not built by this package)

`APP_VENV` must already hold the application's Python environment. Build it with the app's `setup-env.sh` with the `emf_data` step removed (the traced app does not need it),
on a login node: the build takes hours, and compute nodes cannot create files under `python3.N/site-packages`. The isolated verification run says
`NOT EXERCISED` for this step (and for the moirai venv build when it reuses one).

## Expected counts

See `inputs/expected.json` (activities, entities, edges per scale).
"""


def render_batch_files(counts, artifacts, name="app"):
    """Pure: {relative path: text} of the batch-mode scripts, template and README."""
    files = {f"scripts/{n}": '#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n' + b for n, b in BATCH_STEPS.items()}
    files["scripts/run_all.sh"] = '#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n' + RUN_ALL
    files["scripts/check_batch.sh"] = CHECK_BATCH
    files["scripts/" + LOADER] = LIB
    files["scripts/prov_check.py"] = CHECK
    files["templates/submit_moirai.sh.in"] = WRAPPER_TMPL
    files["README.md"] = batch_readme(name, counts, artifacts)
    return files


def write_files(pkg, files):
    for rel, text in files.items():
        p = Path(pkg) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.startswith("scripts/") or rel.startswith("templates/"):
            p.chmod(0o755)


def scan_rendered(files, extra=()):
    """Pure: findings in rendered texts: a literal user, home, session or site path, a job id, a bash -n error. Scripts and templates only."""
    needles = {"/".join(["", "usr", "WS2"]): "workspace path", "/".join(["", "g", "g"]): "home path", "hari" + "dev": "user name"}
    home, user = os.path.expanduser("~"), os.environ.get("USER", "")
    if len(home) > 3:
        needles[home] = "home path"
    if len(user) > 2:
        needles[f"/{user}/"] = "username in a path"
    needles.update({e: "literal value" for e in extra if e})
    found = []
    for rel, text in files.items():
        if not (rel.startswith("scripts/") or rel.startswith("templates/")):
            continue
        found += [f"{rel}: hard-coded {what}" for n, what in needles.items() if n in text]
        if JOBID.search(text):
            found.append(f"{rel}: looks like a job id")
        if rel.endswith(".sh") or rel.endswith(".in"):
            r = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True)
            if r.returncode:
                found.append(f"{rel}: bash -n: {r.stderr.strip()[:160]}")
    return found


def lint(pkg, ws):
    findings = []
    home, user = os.path.expanduser("~"), os.environ.get("USER", "")
    forbidden = {str(ws): "workspace path", home: "home path"}
    if user:
        forbidden[f"/{user}/"] = "username in a path"
    for f in sorted(list(pkg.glob("scripts/*")) + list(pkg.glob("templates/*"))):
        text = f.read_text(errors="replace")
        findings += [f"{f.name}: hard-coded {what}" for needle, what in forbidden.items() if needle and needle in text]
        if JOBID.search(text):
            findings.append(f"{f.name}: looks like a job id")
        if f.suffix == ".sh":
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            if r.returncode:
                findings.append(f"{f.name}: bash -n: {r.stderr.strip()[:160]}")
            if f.name != LOADER and f.name not in STANDALONE and LOADER not in text:
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


def is_batch(outputs_by_stage, mode="auto"):
    """Pure: batch mode when asked, or when the baseline / moirai_smoke outputs record batch_script or submit_command."""
    if mode in ("batch", "generic"):
        return mode == "batch"
    return any((o or {}).get("batch_script") or (o or {}).get("submit_command") for o in outputs_by_stage)


# ------------------------------------------------------------------ sandbox selftest (fake flux, module, dbcast and app python; nothing on the cluster)
_RUN_OUT_FAIL = ("flux-job: task(s) exited with exit code 2\nFri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\n"
                 "flux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n")
_RUN_OUT_OK = "Fri Oct 30 23:30:01 PDT 2026: f35QqToh9 done\nflux-job: there are no more waitable jobs\nAll jobs completed (or failed)!\n"


def _sh(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(0o755)
    return p


def selftest():
    cases = {}
    counts = {"activities": 1, "entities": 1, "edges": 1}
    files = render_batch_files(counts, ["x"], "demo")
    with tempfile.TemporaryDirectory(prefix="repro_batch_selftest_") as td:
        td = Path(td).resolve()
        found = scan_rendered(files, extra=[str(td)])
        cases["no_literal_paths_parse_ok"] = not found
        text04 = files["scripts/04_run.sh"]
        cases["no_service_on_login"] = "moirai-service" not in text04
        cases["wrapper_template_service_in_wrapper"] = "moirai-service start" in files["templates/submit_moirai.sh.in"] and "MOIRAI_DFTRACER_SERVICE" in files["templates/submit_moirai.sh.in"]
        pkg, out = td / "pkg", td / "out"
        write_files(pkg, files)
        splits = td / "splits"
        splits.mkdir()
        (splits / "c1.csv").write_text("a,,,1\nb,,,2\nc,,,3\nd,,,4\n")
        (splits / "c0.csv").write_text("MGYP001796749278,,,134\nMGYP001796749279,,,135\nMGYP001796749280,,,136\n")
        site = td / "mv" / "lib" / "python3.13" / "site-packages"
        for n in ("moirai", "dftracer", "dftracer.libs"):
            (site / n).mkdir(parents=True)
        _sh(site / "dftracer" / "bin" / "dftracer_service", "#!/bin/sh\nexit 0\n")
        _sh(td / "mv" / "bin" / "python", f"#!/bin/sh\necho {site}\n")
        gen_log = td / "gen.log"
        _sh(td / "av" / "bin" / "python", f"""#!/bin/bash
echo "$*" >> {gen_log}
sub=; src=; dst=
while [ $# -gt 0 ]; do case "$1" in --submit-script) sub="$2";; --src-dir) src="$2";; --) dst="$2";; esac; shift; done
printf '#!/bin/bash\\n#flux: --nodes 2\\n#flux: --output {td}/run.out\\nflux bulksubmit --nodes 1 %s {{}} ::: /x.csv\\n# %s\\n' "$sub" "$src" > "$dst"
""")
        (out / "src" / "utils").mkdir(parents=True)
        (out / "src" / "utils" / "gen_launch_script.py").write_text("")
        _sh(out / "src" / "monomer-structure-prediction" / "submit_smsp.sh", "#!/bin/bash\nexit 0\n")
        cfg = td / "config.ini"
        cfg.write_text(batch_config("demo", str(out), str(td / "av"), str(td / "mv"), "", "", {}, str(splits)))
        base_env = {"HOME": str(td), "USER": "tester", "CONFIG": str(cfg), "BASH_ENV": str(_sh(td / "modenv.sh", "module() { return 0; }\n"))}

        def run(script, scale, flux_bin, extra=None):
            env = {**base_env, "SCALE": scale, "PATH": f"{flux_bin}:/usr/bin:/bin"}
            env.update(extra or {})
            return subprocess.run(["bash", str(pkg / "scripts" / script)], env=env, capture_output=True, text=True, timeout=120)

        fb = td / "fakebin-none"
        fb.mkdir()
        r3 = run("03_build.sh", "smoke", fb)
        envf = out / "moirai_env.sh"
        wr = [out / "traces" / s / "submit_moirai.sh" for s in ("smoke", "baseline")]
        cases["03_ran"] = r3.returncode == 0 and envf.exists() and all(w.exists() for w in wr)
        wtext = wr[0].read_text() if wr[0].exists() else ""
        cases["03_wrapper_rendered"] = bool(wtext) and not re.search(r"@[A-Z_]+@", wtext) and str(td / "mv") in wtext and str(out / "traces" / "smoke") in wtext and "/moirai-repro-" in wtext
        cases["03_wrapper_parses"] = bool(wtext) and subprocess.run(["bash", "-n", str(wr[0])], capture_output=True).returncode == 0
        cases["03_wrapper_per_scale"] = len(wr) == 2 and wr[1].exists() and str(out / "traces" / "baseline") in wr[1].read_text() and str(out / "traces" / "smoke") not in wr[1].read_text()
        etext = envf.read_text() if envf.exists() else ""
        cases["03_env_shim_and_src"] = f"PYTHONPATH={out / 'moirai_shim'}:{out / 'src'}" in etext and (out / "moirai_shim" / "moirai").is_symlink() and "MOIRAI_SERVICE=" in etext
        cases["03_env_parses"] = bool(etext) and subprocess.run(["bash", "-n", str(envf)], capture_output=True).returncode == 0

        def flux_for(name, scale, run_out, hosts_text, result="COMPLETED", rc="0"):
            fbd = td / name
            stage = out / "traces" / scale
            hosts_cmd = f"echo {hosts_text} > {stage}/hosts.txt" if hosts_text else ":"
            body = f"""#!/bin/bash
case "$1" in
  batch) echo fFAKEJOB ;;
  watch) cp {td}/runout.src {td}/run.out 2>/dev/null; {hosts_cmd} ;;
  jobs) case "$*" in *returncode*) echo {rc} ;; *result*) echo {result} ;; esac ;;
  cancel) exit 0 ;;
esac
"""
            _sh(fbd / "flux", body)
            _sh(fbd / "dbcast", "#!/bin/sh\nexit 0\n")
            (td / "runout.src").write_text(run_out)
            return fbd

        r = run("04_run.sh", "smoke", flux_for("f_fail", "smoke", _RUN_OUT_FAIL, "nodeA"))
        cases["04_smoke_failure_detected"] = r.returncode == 4 and "f35QqToh9" in r.stderr and "FAILED" in r.stderr
        smoke_in = out / "inputs" / "smoke_chunks"
        made = sorted(smoke_in.glob("*")) if smoke_in.exists() else []
        cases["04_smoke_input_two_lines"] = len(made) == 1 and made[0].name == "c0.csv" and made[0].read_text() == "MGYP001796749278,,,134\nMGYP001796749279,,,135\n"
        g = gen_log.read_text() if gen_log.exists() else ""
        for flag in ("--config configs/tuolumne_test.yaml", f"--inchunks {smoke_in}", f"--submit-script {out / 'traces' / 'smoke' / 'submit_moirai.sh'}", f"--src-dir {out / 'src'}",
                     f"--env-dir {td / 'av'}", f"--prestage-script {out / 'src' / 'monomer-structure-prediction' / 'prestage_check.sh'}", "--overwrite", f"--cwd {out / 'src' / 'monomer-structure-prediction'}"):
            cases["04_gen_flag " + flag.split()[0]] = flag in g
        r = run("04_run.sh", "smoke", flux_for("f_ok", "smoke", _RUN_OUT_OK, "nodeA"))
        cases["04_smoke_clean_pass"] = r.returncode == 0 and "clean" in r.stdout
        r = run("04_run.sh", "baseline", flux_for("f_ok2", "baseline", _RUN_OUT_OK, "nodeA"))
        gl = (gen_log.read_text() if gen_log.exists() else "").strip().splitlines()
        cases["04_baseline_full_split"] = r.returncode == 0 and bool(gl) and f"--inchunks {splits} " in gl[-1] + " "
        r = run("04_run.sh", "baseline", flux_for("f_nohost", "baseline", _RUN_OUT_OK, ""))
        cases["04_no_hosts_exit5"] = r.returncode == 5 and "no per-chunk wrapper ran" in r.stderr
        r = run("04_run.sh", "baseline", flux_for("f_batchfail", "baseline", _RUN_OUT_OK, "nodeA", result="FAILED", rc="1"))
        cases["04_batch_job_failed"] = r.returncode == 1 and "FAILED" in r.stderr
        r = run("04_run.sh", "baseline", fb)
        cases["04_needs_flux_dbcast"] = r.returncode != 0 and ("dbcast" in r.stderr or "flux" in r.stderr)
        r = run("04_run.sh", "bogus", fb)
        cases["04_bad_scale"] = r.returncode != 0

        # check_batch.sh on its own: the real failing and clean run.out text
        cb = pkg / "scripts" / "check_batch.sh"
        (td / "h_ok.txt").write_text("nodeA\n")
        (td / "h_empty.txt").write_text("")
        (td / "o_fail.txt").write_text(_RUN_OUT_FAIL)
        (td / "o_ok.txt").write_text(_RUN_OUT_OK)
        (td / "o_code.txt").write_text("flux-job: task(s) exited with exit code 2\n")
        (td / "o_zero.txt").write_text("flux-job: task(s) exited with exit code 0\n")

        def chk(o, h):
            return subprocess.run(["bash", str(cb), str(o), str(h), "fX"], capture_output=True, text=True, timeout=30)
        p = chk(td / "o_fail.txt", td / "h_ok.txt")
        cases["check_failing_run_out_exit4"] = p.returncode == 4 and "f35QqToh9" in p.stderr
        cases["check_exit_code_only_exit4"] = chk(td / "o_code.txt", td / "h_ok.txt").returncode == 4
        cases["check_exit_code_zero_clean"] = chk(td / "o_zero.txt", td / "h_ok.txt").returncode == 0
        p = chk(td / "o_ok.txt", td / "h_ok.txt")
        cases["check_clean_run_out_exit0"] = p.returncode == 0 and "clean" in p.stdout
        cases["check_empty_hosts_exit5"] = chk(td / "o_ok.txt", td / "h_empty.txt").returncode == 5
        cases["check_missing_hosts_exit5"] = chk(td / "o_ok.txt", td / "nope.txt").returncode == 5
        cases["check_unreadable_output_exit6"] = chk(td / "nope.out", td / "h_ok.txt").returncode == 6
        cases["check_no_output_header_hosts_ok"] = subprocess.run(["bash", str(cb), "", str(td / "h_ok.txt")], capture_output=True, text=True).returncode == 0
        cases["generic_mode_unchanged"] = set(STEPS) == {"00_check_prereqs.sh", "01_get_source.sh", "02_setup_env.sh", "03_build.sh", "04_run.sh", "05_validate.sh"} and "moirai-service start" in STEPS["04_run.sh"]
        cases["mode_detect"] = (is_batch([{"batch_script": "/x/run.sh"}]) and not is_batch([{"command": "x"}]) and is_batch([{}], "batch") and not is_batch([{"submit_command": "flux batch x"}], "generic"))
        cases["config_batch_mode"] = "MODE=batch" in cfg.read_text() and "VENV=${MOIRAI_VENV}" in cfg.read_text()
    bad = [k for k, v in cases.items() if not v]
    line = "repro batch selftest PASS " + " ".join(f"{k}=ok" for k in cases) if not bad else "repro batch selftest FAIL " + ",".join(bad)
    return {"status": "ok" if not bad else "error", "line": line, "cases": cases, "findings": found}


def register(mcp):
    @mcp.tool()
    def repro_build(pipeline: str, output_root: str = "${HOME}/moirai_repro/${APP}", mode: str = "auto", app_venv: str = "", moirai_venv: str = "", queue: str = "", chunk_dir: str = "") -> dict:
        """Generate final_provenance/ in the session: config.ini, scripts, the provenance patch, the spec, pinned versions and a standalone checker. Use after the query stage, or again after
        a fix: it starts over. output_root: where a reader's run writes; keep ${USER} or ${HOME}, never a literal name. mode: auto (batch when the baseline or moirai_smoke outputs record
        batch_script or submit_command, else generic), batch or generic. Batch mode (a generated flux batch run.sh): app_venv the existing application venv (default read from run.sh's
        ELMER_ENV / --env-dir), moirai_venv the venv holding moirai (default the baseline's service_venv), queue (default the baseline's), chunk_dir the split (default --inchunks of the
        pipeline command); site paths are written with ${HOME} / ${USER}. Returns {status, package, mode, files, source, pins, warnings}."""
        c = P.load(pipeline)
        integ, ann, base = P.stage(c, "integrate")["outputs"], P.stage(c, "annotate")["outputs"], P.stage(c, "baseline")["outputs"]
        smoke_run = P.stage(c, "moirai_smoke")["outputs"]
        if not ann.get("spec") or not smoke_run.get("traces"):
            return {"status": "error", "error": "needs the annotate and moirai_smoke stages"}
        batch = is_batch([base, smoke_run], mode)
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
        if url and not re.fullmatch(r"[0-9a-f]{40}", ref):
            warnings.append(f"APP_REF {ref!r} is not a full 40-character SHA")
        app = re.sub(r"\W", "_", c["name"])
        counts = expected.get("baseline") or expected["smoke"]
        if batch:
            rs_path = base.get("batch_script") or smoke_run.get("batch_script") or ""
            rs_text = Path(rs_path).read_text(errors="replace") if rs_path and Path(rs_path).is_file() else ""
            cmd = base.get("command") or smoke_run.get("command") or c.get("command") or ""
            m = re.search(r"ELMER_ENV=(\S+)", rs_text) or re.search(r"--env-dir[= ](\S+)", rs_text)
            av = app_venv or (m.group(1).strip("'\"") if m and "<" not in m.group(1) else "")
            if not av:
                av = "${OUTPUT_ROOT}/app-venv"
                warnings.append("could not find the application venv: set APP_VENV in config.ini (it must already exist)")
            mv = moirai_venv or base.get("service_venv") or smoke_run.get("service_venv") or ""
            m = re.search(r"--inchunks[= ](\S+)", cmd)
            cd = chunk_dir or (m.group(1) if m else "")
            if not cd:
                warnings.append("could not find the chunk dir: set CHUNK_DIR in config.ini")
            m = re.search(r"--config[= ](\S+)", cmd)
            q = queue or base.get("queue") or smoke_run.get("queue") or "pdebug"
            cfg_text = batch_config(app, _site(output_root), _site(av), _site(mv) if mv else "${OUTPUT_ROOT}/moirai-venv", url, ref, pins, _site(cd), m.group(1) if m else "configs/tuolumne_test.yaml", q)
            write_files(pkg, render_batch_files(counts, ann["spec"].get("scientific_artifacts", []), c["name"]))
            (pkg / "config.ini").write_text(cfg_text)
        else:
            cfg = ["# The ONLY file with site or user values. Every script sources scripts/lib_load_config.sh, which reads it. The environment overrides it.", "", "[paths]",
                   f"APP={app}", f"OUTPUT_ROOT={_site(output_root)}", "VENV=${OUTPUT_ROOT}/env", "# 1 uses an existing venv with moirai in it.", "REUSE_VENV=0", "", "[repos]",
                   f"APP_URL={url}", f"APP_REF={ref}", f"DFTRACER_SPEC={pins.get('dftracer', '')}", f"MOIRAI_SPEC={pins.get('moirai', '')}", f"UTILS_SPEC={pins.get('dftracer-utils', '')}", "", "[run]",
                   "# smoke or baseline.", "SCALE=smoke", f"COMMAND_smoke={_site(smoke_run.get('command') or c['command'])}", f"COMMAND_baseline={_site(base.get('command') or c['command'])}",
                   f"LAUNCHER={base.get('launcher', '')}", f"BUILD_COMMAND={_site(integ.get('build_command') or '')}", ""]
            (pkg / "config.ini").write_text("\n".join(cfg))
            (pkg / "scripts" / LOADER).write_text(LIB)
            (pkg / "scripts" / "prov_check.py").write_text(CHECK)
            head = '#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n'
            for name, body in STEPS.items():
                (pkg / "scripts" / name).write_text(head + body)
            (pkg / "scripts" / "run_all.sh").write_text(head + RUN_ALL)
            for f in (pkg / "scripts").iterdir():
                f.chmod(0o755)
            (pkg / "README.md").write_text(f"""# {c['name']}: provenance reproduction package

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
""")
        files = sorted(str(f.relative_to(pkg)) for f in pkg.rglob("*") if f.is_file())
        P.record(pipeline, STAGE, status="running", outputs={"package": str(pkg), "mode": "batch" if batch else "generic", "files": len(files), "pins": pins, "warnings": warnings, "verified": None,
                                                              "summary": f"{len(files)} files in final_provenance ({'batch' if batch else 'generic'} mode)"}, note="package built")
        return {"status": "ok", "package": str(pkg), "mode": "batch" if batch else "generic", "files": files, "source": url or "bundled", "pins": pins, "warnings": warnings}

    @mcp.tool()
    def repro_batch_render() -> dict:
        """Pure, no pipeline: the texts of the batch-mode package, to read or review. Returns {status, scripts: {name: text}, wrapper_template, check_batch, findings}: 04_run.sh is
        the same text for smoke and baseline (SCALE picks the input and walltime at run time); findings lists literal user/session paths, job ids and bash -n errors (must be empty)."""
        files = render_batch_files({"activities": 0, "entities": 0, "edges": 0}, [], "app")
        return {"status": "ok", "scripts": {k.split("/", 1)[1]: v for k, v in files.items() if k.startswith("scripts/") and k.endswith(".sh")}, "wrapper_template": WRAPPER_TMPL,
                "check_batch": CHECK_BATCH, "findings": scan_rendered(files)}

    @mcp.tool()
    def repro_batch_selftest() -> dict:
        """Sandbox test of batch mode, nothing on the cluster: renders the package into a temp dir, runs 03_build.sh (env file, shim, per-scale wrappers) and 04_run.sh for smoke and baseline
        under bash with a fake flux, module, dbcast and application python, and check_batch.sh on the real failing run.out text (`exited with exit code 2`, `... failed`: exit 4), a clean one (0),
        a missing/empty hosts.txt (5) and an unreadable output (6); no literal user/session path in any rendered script; no moirai-service on the login node; generic mode unchanged.
        Use after any change to the batch texts. Returns {status, line, cases, findings}: line is 'repro batch selftest PASS ...' or 'repro batch selftest FAIL <cases>'."""
        return selftest()

    @mcp.tool()
    def repro_verify(pipeline: str, isolation_root: str, scale: str = "smoke", reuse_venv: str = "", limit: int = 3600, wait: int = 300, app_venv: str = "") -> dict:
        """Prove the package works: copy it to a new folder outside the session and run it in a clean login shell, with nothing inherited (env -i, bash -l). Use after repro_build. isolation_root:
        a parent folder outside the session, on a file system a compute node can see. scale: smoke or baseline. reuse_venv: a venv to reuse instead of building one (batch mode: the moirai venv).
        app_venv: batch mode only, the existing application venv (else the one in config.ini). limit: seconds (batch mode: the job is cancelled at the limit). wait: seconds to wait here.
        Batch mode never starts a service on the login node; the log says what was NOT EXERCISED. Returns {status, job, iso_dir, mode, log_tail}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        root = Path(os.path.expanduser(isolation_root)).resolve()
        if str(root).startswith(str(P.work(c).parent.resolve()) + os.sep):
            return {"status": "error", "error": "isolation_root must be outside the session workspace"}
        iso = root / f"isolation_{time.strftime('%Y%m%d_%H%M%S')}"
        iso.mkdir(parents=True)
        shutil.copytree(o["package"], iso / "pkg", symlinks=True)
        batch = o.get("mode") == "batch"
        env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
        env.update(TERM="dumb", OUTPUT_ROOT=str(iso / "out"), SCALE=scale)
        if reuse_venv:
            env.update(REUSE_VENV="1", VENV=reuse_venv)
            if batch:
                env.update(MOIRAI_VENV=reuse_venv)
        if batch and app_venv:
            env.update(APP_VENV=app_venv)
        cmd = f"cd {shlex.quote(str(iso))} && bash pkg/scripts/run_all.sh"
        args = " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
        j = J.start(pipeline, STAGE, "isolated-run", f"env -i {args} bash -lc {shlex.quote(cmd)}\n", str(iso), limit=limit)
        P.record(pipeline, STAGE, outputs={"verify": {"job": j["id"], "iso_dir": str(iso), "scale": scale, "mode": "batch" if batch else "generic", "reuse_venv": bool(reuse_venv)}},
                 note=f"isolated run of {scale} in {iso}")
        r = J.wait(pipeline, j["id"], wait)
        return {"status": r["status"], "job": j["id"], "rc": r["rc"], "iso_dir": str(iso), "mode": "batch" if batch else "generic", "log_tail": r.get("log_tail", "")[-2500:]}

    @mcp.tool()
    def repro_validate(pipeline: str) -> dict:
        """Validate the package: it holds no user, workspace or job path, its scripts parse and source the loader, and an isolated run passed with the original counts. Use before the last gate.
        Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if not v.check("built", bool(o.get("package")), "repro_build has not run", "run repro_build"):
            return P.finish_validation(pipeline, v)
        found = lint(Path(o["package"]), P.work(c).parent.resolve())
        v.check("portable", not found, "; ".join(found)[:380], "move the value to config.ini with ${HOME} or ${USER}, then repro_build")
        v.check("pinned", bool(o.get("pins", {}).get("moirai")), f"pins: {o.get('pins')}", "pin moirai and dftracer in config.ini")
        vf = o.get("verify")
        if not vf:
            v.inconclusive("isolated_run", "repro_verify has not run")
            return P.finish_validation(pipeline, v)
        j = J.get(pipeline, vf["job"], 1500)
        if j["status"] == "running":
            v.inconclusive("isolated_run", "still running")
            return P.finish_validation(pipeline, v)
        v.check("isolated_run", j["rc"] == 0, f"rc={j['rc']}: {j.get('log_tail', '')[-300:]}", "read the log: something the session provided is missing from the package (batch: exit 4/5/6 are in the skill's Corrector table)")
        run = Path(vf["iso_dir"]) / "out" / "run.json"
        if v.check("reproduced", run.exists() and json.loads(run.read_text()).get("ok"), run.read_text()[:300] if run.exists() else "no run.json", "the counts differ: is the app deterministic? Is the input the same?"):
            note = ""
            if vf.get("mode") == "batch":
                note = f" (scale {vf.get('scale')}; application environment build not exercised" + ("; moirai venv reused" if vf.get("reuse_venv") else "") + ")"
            P.record(pipeline, STAGE, outputs={"summary": f"reproduced in a clean shell: {json.loads(run.read_text())['counts']}{note}"})
        return P.finish_validation(pipeline, v)
