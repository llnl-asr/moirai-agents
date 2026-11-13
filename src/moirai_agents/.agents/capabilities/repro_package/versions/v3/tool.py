"""Last stage of the run pipeline: final_provenance/, a folder that reproduces the run for someone who has neither this session nor this account, and the proof that it does.

Same shape as dftracer-agents' reproduction packages: config.ini is the only file with site values, every script sources lib_load_config.sh, the instrumentation is a patch
against a pinned app, and a standalone checker judges the result. It is verified by running it from a fresh copy in a clean login shell.

Two modes. generic: the app runs in the shell under moirai-service. batch: the app is launched by a generated `flux batch` run.sh whose per-chunk submit wrapper starts
moirai-service on the worker node (Elmerfold); the package then generates run.sh, submits it, waits for it and judges its output, and starts no service on the login node.
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

# ---- batch mode -----------------------------------------------------------------------------------------------------------------------------------------
SCAN_SH = r'''scan_run_out() {
  # A batch job's return code does not reflect its chunk jobs: judge its --output file. Prints each fault, returns 1 on any.
  local f="$1" bad=0
  if [[ ! -s "$f" ]]; then echo "batch output missing or empty: $f"; return 1; fi
  if grep -Eq 'exited with exit code [1-9][0-9]*' "$f"; then echo "a task exited non-zero: $(grep -E 'exited with exit code [1-9][0-9]*' "$f" | sed -n 1p)"; bad=1; fi
  if grep -Eq ' failed[[:space:]]*$' "$f"; then echo "a chunk job failed: $(grep -E ' failed[[:space:]]*$' "$f" | sed -n 1p)"; bad=1; fi
  return "$bad"
}
'''
WRAPPER_T = r'''#!/bin/bash
# Per-chunk submit wrapper, rendered by 03_build.sh. flux bulksubmit runs it on the exclusive worker node as: submit_moirai.sh <chunk csv>
# It starts moirai-service in the background of THIS shell (never via flux run/submit), runs the original submit script as a child,
# and stops the service with the same environment. Arguments are passed to the original unchanged.
ORIG="@ORIG@"
ENVF="@ENVF@"
STAGE_DIR="@STAGE@"
[ -f "${ORIG}" ] || { echo "moirai-wrapper: original submit script ${ORIG} not found" >&2; exit 2; }
[ -f "${ENVF}" ] || { echo "moirai-wrapper: environment file ${ENVF} not found" >&2; exit 2; }
source "${ENVF}" || { echo "moirai-wrapper: sourcing ${ENVF} failed" >&2; exit 2; }
command -v moirai-service >/dev/null 2>&1 || { echo "moirai-wrapper: moirai-service not on PATH after sourcing ${ENVF}" >&2; exit 2; }
_job="${FLUX_JOB_ID:-$$}"
_host="$(hostname)"
export MOIRAI_LOG_FILE="@TRACES@/prov"
export MOIRAI_DATA_DIR="@DATA@"
export MOIRAI_SERVICE_SHM="@SHM_BASE@-${_job}"
export MOIRAI_PC_BLOCKS=${MOIRAI_PC_BLOCKS:-16}
export MOIRAI_PC_FLUSH_MS=${MOIRAI_PC_FLUSH_MS:-250}
# moirai-service --help prints a build-time default for dftracer_service; when it does not exist, point to the installed one
if [ -z "${MOIRAI_DFTRACER_SERVICE:-}" ]; then
  _dflt="$(moirai-service --help 2>&1 | grep -o '/[^[:space:]]*dftracer_service' | head -1)"
  if [ -z "${_dflt}" ] || [ ! -x "${_dflt}" ]; then
    _real="$(readlink -f "$(command -v moirai-service)")"
    for _c in "@MOIRAI_VENV@"/lib/python*/site-packages/dftracer/bin/dftracer_service "$(dirname "${_real}")/../../dftracer/bin/dftracer_service"; do
      if [ -x "${_c}" ]; then export MOIRAI_DFTRACER_SERVICE="$(readlink -f "${_c}")"; break; fi
    done
    [ -n "${MOIRAI_DFTRACER_SERVICE:-}" ] || { echo "moirai-wrapper: no dftracer_service found (default ${_dflt:-unknown} missing)" >&2; exit 2; }
  fi
fi
_svclog="${STAGE_DIR}/service-${_host}-${_job}.log"
echo "${_host}" >> "${STAGE_DIR}/hosts.txt"
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
BATCH_STEPS = {
    "00_check_prereqs.sh": r'''# Commands, and that the runtime this package needs can be reached.
for c in git patch python3 flux; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 1; }; done
if [[ "$REUSE_VENV" == 1 ]]; then
  [[ -x "$PY" ]] || { echo "REUSE_VENV=1 but the app venv has no $PY" >&2; exit 1; }
  [[ -x "$MOIRAI_VENV/bin/moirai-service" ]] || { echo "REUSE_VENV=1 but $MOIRAI_VENV has no moirai-service" >&2; exit 1; }
fi
log "ok"
''',
    "02_setup_env.sh": r'''# Two venvs. The moirai venv is built here. The APP venv is NOT: see README.md (setup-env.sh without the emf_data step, a login-node build, hours).
# REUSE_VENV=1 reuses both (VENV = app venv, MOIRAI_VENV = moirai venv) and the install is then not exercised.
if [[ "$REUSE_VENV" == 1 ]]; then log "reusing app venv $VENV and moirai venv $MOIRAI_VENV; the environment build is NOT exercised"; exit 0; fi
[[ -x "$PY" ]] || { echo "the app venv $VENV does not exist: build it first (README.md, section App environment)" >&2; exit 1; }
python3 -m venv "$MOIRAI_VENV"
"$MOIRAI_VENV/bin/python" -m pip install -q --upgrade pip scikit-build-core pybind11
DFTRACER_WHEEL=1 "$MOIRAI_VENV/bin/python" -m pip install -q "$DFTRACER_SPEC"
"$MOIRAI_VENV/bin/python" -m pip install -q --no-build-isolation "$MOIRAI_SPEC"
[[ -z "$UTILS_SPEC" ]] || "$MOIRAI_VENV/bin/python" -m pip install -q "$UTILS_SPEC"
''',
    "03_build.sh": r'''# Environment file, symlink shim and per-chunk wrapper. The app runs under its own venv python with NO moirai installed in it.
SITE="$(ls -d "$MOIRAI_VENV"/lib/python*/site-packages | sed -n 1p)"
[[ -n "$SITE" ]] || { echo "no site-packages in $MOIRAI_VENV" >&2; exit 1; }
STAGE="$OUTPUT_ROOT/traces/$SCALE"; SHIM="$OUTPUT_ROOT/moirai_shim"; ENVF="$OUTPUT_ROOT/moirai_env.sh"
mkdir -p "$STAGE" "$SHIM"
for n in moirai dftracer dftracer.libs; do
  if [[ -e "$SITE/$n" ]]; then ln -sfn "$SITE/$n" "$SHIM/$n"; fi
done
{
  echo "# Generated by 03_build.sh: the environment of the annotated app."
  echo "export MOIRAI_SERVICE=$MOIRAI_VENV/bin/moirai-service"
  echo "export PATH=\"\$PATH\":$MOIRAI_VENV/bin"
  echo "export PYTHONPATH=$SHIM:$SRC\"\${PYTHONPATH:+:\$PYTHONPATH}\""
  echo "export LD_LIBRARY_PATH=\"\${LD_LIBRARY_PATH:+\$LD_LIBRARY_PATH:}\"$SITE/moirai/lib64:$SITE/dftracer/lib64:$SITE/dftracer/lib"
} > "$ENVF"
sed -e "s|@ORIG@|$SRC/$APP_SUBDIR/$ORIG_SUBMIT|g" -e "s|@ENVF@|$ENVF|g" -e "s|@STAGE@|$STAGE|g" -e "s|@TRACES@|$TRACES|g" \
    -e "s|@DATA@|$SRC|g" -e "s|@SHM_BASE@|/moirai-repro-$SCALE|g" -e "s|@MOIRAI_VENV@|$MOIRAI_VENV|g" \
    "$PKG_DIR/templates/submit_moirai.sh.in" > "$STAGE/submit_moirai.sh"
chmod +x "$STAGE/submit_moirai.sh"
bash -n "$STAGE/submit_moirai.sh"
log "wrapper $STAGE/submit_moirai.sh, env $ENVF"
''',
    "04_run.sh": SCAN_SH + r'''# Generate run.sh with the app's own generator, submit it as ONE flux batch job, wait, judge its output. No provenance service runs on this node:
# the wrapper starts it on each worker node.
set +u
if type module >/dev/null 2>&1; then module load $MODULES || { echo "module load $MODULES failed" >&2; exit 1; }; else echo "no module command: run from a login shell" >&2; exit 1; fi
set -u
require VENV MOIRAI_VENV SPLITS_DIR APP_SUBDIR ORIG_SUBMIT PRESTAGE CONFIG_YAML QUEUE TIME_LIMIT
APPDIR="$SRC/$APP_SUBDIR"
STAGE="$OUTPUT_ROOT/traces/$SCALE"
SUBMIT="$STAGE/submit_moirai.sh"
[[ -x "$SUBMIT" ]] || { echo "no wrapper $SUBMIT: run 03_build.sh first" >&2; exit 1; }
[[ -x "$PY" ]] || { echo "no app venv python $PY" >&2; exit 1; }
case "$SCALE" in
  smoke)
    INCH="$OUTPUT_ROOT/inchunks/smoke"; rm -rf "$INCH"; mkdir -p "$INCH"
    first="$(find "$SPLITS_DIR" -maxdepth 1 -name '*.csv' | sort | sed -n 1p)"
    [[ -n "$first" ]] || { echo "no chunk csv in $SPLITS_DIR" >&2; exit 1; }
    head -n "${SMOKE_LINES:-2}" "$first" > "$INCH/$(basename "$first")"
    log "smoke input: first ${SMOKE_LINES:-2} lines of $(basename "$first") in $INCH";;
  baseline) INCH="$SPLITS_DIR";;
  *) echo "SCALE must be smoke or baseline, not $SCALE" >&2; exit 2;;
esac
OUT="$OUTPUT_ROOT/app_out/$SCALE"; mkdir -p "$OUT/timing" "$OUT/logs"
RUN_SH="$STAGE/run.sh"; HOSTS="$STAGE/hosts.txt"
rm -f "$HOSTS" "$STAGE/run.out" "$RUN_SH"
(cd "$SRC" && "$PY" utils/gen_launch_script.py --config "$CONFIG_YAML" --inchunks "$INCH" --submit-script "$SUBMIT" --src-dir "$SRC" \
   --output-dir "$OUT" --env-dir "$VENV" --timing-dir "$OUT/timing" --log-dir "$OUT/logs" --prestage-script "$APPDIR/$PRESTAGE" \
   --overwrite --cwd "$APPDIR" -- "$RUN_SH")
[[ -s "$RUN_SH" ]] || { echo "gen_launch_script.py did not write $RUN_SH" >&2; exit 1; }
RUN_OUT="$(awk '/^#flux: *--output/ {print $NF; exit}' "$RUN_SH")"
args=(-t "$TIME_LIMIT" --queue "$QUEUE")
if [[ -z "$RUN_OUT" ]]; then RUN_OUT="$STAGE/run.out"; args+=(--output "$RUN_OUT"); fi
JOB="$(flux batch "${args[@]}" "$RUN_SH")"
[[ -n "$JOB" ]] || { echo "flux batch returned no job id" >&2; exit 1; }
log "batch job $JOB submitted (scale $SCALE, output $RUN_OUT); waiting"
flux watch "$JOB" || log "flux watch returned non-zero (judged from the output below)"
fail=0
scan_run_out "$RUN_OUT" >&2 || fail=1
if [[ ! -s "$HOSTS" ]]; then echo "no worker wrote $HOSTS: the wrapper never ran" >&2; fail=1; fi
[[ "$fail" == 0 ]] || { echo "batch run failed: see $RUN_OUT" >&2; exit 1; }
log "batch job finished clean; traces in $TRACES"
''',
}
BATCH_README = r'''## App environment (not built by these scripts)

`02_setup_env.sh` builds only the moirai venv. The application venv (`VENV`) must exist already, and `REUSE_VENV=1` reuses both venvs, in which case
the environment build is NOT exercised. To build the app venv:

- Run the app's `setup-env.sh` with the `emf_data` step removed, because that step needs data this package does not carry.
- Build on a login node: it takes hours. Compute nodes cannot create files under `python3.N/site-packages`, so a build there fails.
- The app never imports moirai from its own venv: `03_build.sh` writes `moirai_env.sh` and a symlink shim (`moirai`, `dftracer`, `dftracer.libs`) from the moirai venv.

## Batch run

`04_run.sh` loads `MODULES` (mpifileutils, so `dbcast` is on PATH), makes the smoke input (the first `SMOKE_LINES` lines of the first chunk of `SPLITS_DIR`; baseline uses the
whole split), generates `run.sh` with the app's `utils/gen_launch_script.py`, submits it with `flux batch`, runs `flux watch`, then fails when the batch output holds
`exited with exit code <non-zero>` or a line ending ` failed`, or when `hosts.txt` is missing or empty. A batch job's return code does not reflect its chunk jobs. Expect about 8 minutes
at either scale (a ~270 s dbcast dominates). `moirai-service` runs only on the worker nodes, started by the per-chunk wrapper rendered from `templates/submit_moirai.sh.in`.
'''


def _batch_cfg(output_root, command_text, url, ref, pins):
    """config.ini lines of a batch package; site values only."""
    def grab(flag, default):
        m = re.search(rf"{flag}\s+(\S+)", command_text)
        return m.group(1) if m else default
    sub = Path(grab("--submit-script", "monomer-structure-prediction/submit_smsp.sh"))
    pre = Path(grab("--prestage-script", "monomer-structure-prediction/prestage_check.sh"))
    return [
        "# The ONLY file with site or user values. Every script sources scripts/lib_load_config.sh, which reads it. The environment overrides it.", "",
        "[paths]", "APP=app", f"OUTPUT_ROOT={_site(output_root)}", "# the app venv (built by the app's setup-env.sh, see README.md) and the moirai venv",
        "VENV=${OUTPUT_ROOT}/env", "MOIRAI_VENV=${OUTPUT_ROOT}/moirai-venv", "# 1 reuses both venvs; the environment build is then not exercised.", "REUSE_VENV=0", "",
        "[repos]", f"APP_URL={url}", f"APP_REF={ref}", f"DFTRACER_SPEC={pins.get('dftracer', '')}", f"MOIRAI_SPEC={pins.get('moirai', '')}", f"UTILS_SPEC={pins.get('dftracer-utils', '')}", "",
        "[run]", "MODE=batch", "# smoke or baseline.", "SCALE=smoke", "MODULES=mpifileutils/0.11.1", f"QUEUE={grab('--queue', 'pdebug')}", f"TIME_LIMIT={grab(r'flux batch(?: [^;]*?)? -t', '1h')}",
        f"SPLITS_DIR={_site(grab('--inchunks', ''))}", "SMOKE_LINES=2", f"CONFIG_YAML={grab('--config', 'configs/tuolumne_test.yaml')}",
        f"APP_SUBDIR={sub.parent.name or 'monomer-structure-prediction'}", f"ORIG_SUBMIT={sub.name}", f"PRESTAGE={pre.name}", "BUILD_COMMAND=", "",
    ]


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


def _forbidden(ws):
    home, user = os.path.expanduser("~"), os.environ.get("USER", "")
    forbidden = {str(ws): "workspace path", home: "home path"}
    if user:
        forbidden[f"/{user}/"] = "username in a path"
    return forbidden


def lint(pkg, ws):
    findings = []
    forbidden = _forbidden(ws)
    user = os.environ.get("USER", "")
    for f in sorted(pkg.glob("scripts/*")) + sorted(pkg.glob("templates/*")):
        text = f.read_text(errors="replace")
        findings += [f"{f.name}: hard-coded {what}" for needle, what in forbidden.items() if needle and needle in text]
        if f.parent.name != "scripts":
            continue
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


def _is_batch(pkg):
    cfg = Path(pkg) / "config.ini"
    return cfg.exists() and re.search(r"^MODE=batch\s*$", cfg.read_text(), re.M) is not None


def _write_batch_scripts(pkg, head):
    """Overlay the batch scripts on a package that has the generic ones."""
    for name, body in BATCH_STEPS.items():
        (pkg / "scripts" / name).write_text(head + body)
    (pkg / "templates").mkdir(exist_ok=True)
    (pkg / "templates" / "submit_moirai.sh.in").write_text(WRAPPER_T)


def _scan(text, tmp):
    """Run the package's own scan_run_out on text. Returns (failed, output)."""
    lib = Path(tmp) / "scan.sh"
    lib.write_text(SCAN_SH)
    f = Path(tmp) / "scan_input.out"
    f.write_text(text)
    r = subprocess.run(["bash", "-c", 'source "$1"; scan_run_out "$2"', "scan", str(lib), str(f)], capture_output=True, text=True, timeout=30)
    return r.returncode != 0, (r.stdout + r.stderr).strip()


def _selftest_case(root, scale, case):
    """Run the real 04_run.sh with stub flux, module and app python; returns a dict of what happened."""
    case_dir = Path(root) / f"{scale}_{case}"
    pkg, out, bind, splits = case_dir / "pkg", case_dir / "out", case_dir / "bin", Path(root) / "splits"
    (pkg / "scripts").mkdir(parents=True)
    (case_dir / "venv" / "bin").mkdir(parents=True)
    bind.mkdir()
    (pkg / "scripts" / LOADER).write_text(LIB)
    (pkg / "scripts" / "04_run.sh").write_text('#!/bin/bash\nsource "$(dirname "$0")/' + LOADER + '"\n' + BATCH_STEPS["04_run.sh"])
    (pkg / "config.ini").write_text("\n".join([
        "[paths]", f"OUTPUT_ROOT={out}", f"VENV={case_dir / 'venv'}", f"MOIRAI_VENV={case_dir / 'mvenv'}", "[run]", "MODE=batch", f"SCALE={scale}", "MODULES=mpifileutils/0.11.1",
        "QUEUE=pdebug", "TIME_LIMIT=1h", f"SPLITS_DIR={splits}", "SMOKE_LINES=2", "CONFIG_YAML=configs/c.yaml", "APP_SUBDIR=app", "ORIG_SUBMIT=orig.sh", "PRESTAGE=pre.sh", ""]))
    stage = out / "traces" / scale
    stage.mkdir(parents=True)
    (stage / "submit_moirai.sh").write_text("#!/bin/bash\n")
    (stage / "submit_moirai.sh").chmod(0o755)
    (out / "src" / "utils").mkdir(parents=True)
    runout = {"exit_code": "flux-job: task(s) exited with exit code 2\n", "failed_line": "Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\n"}.get(case, "chunk finished\n")
    (bind / "flux").write_text('#!/bin/bash\ncase "$1" in\n  batch) echo "$@" > "$STUB_DIR/batch.args"; echo fTestJob001;;\n  watch) printf %s "$STUB_RUNOUT" > "$STUB_STAGE/run.out"\n'
                               '    if [ "$STUB_CASE" != no_hosts ]; then echo node1 > "$STUB_STAGE/hosts.txt"; fi;;\nesac\n')
    (case_dir / "venv" / "bin" / "python").write_text('#!/bin/bash\necho "$@" > "$STUB_DIR/gen.args"\necho "#!/bin/bash" > "${@: -1}"\n')
    for f in (bind / "flux", case_dir / "venv" / "bin" / "python"):
        f.chmod(0o755)
    env = {"PATH": f"{bind}:/usr/bin:/bin", "HOME": str(root), "STUB_DIR": str(case_dir), "STUB_STAGE": str(stage), "STUB_CASE": case, "STUB_RUNOUT": runout}
    r = subprocess.run(["bash", "-c", 'module() { :; }; export -f module; exec bash "$0"', str(pkg / "scripts" / "04_run.sh")], capture_output=True, text=True, env=env, timeout=60, cwd=str(case_dir))
    gen = (case_dir / "gen.args").read_text() if (case_dir / "gen.args").exists() else ""
    batch = (case_dir / "batch.args").read_text() if (case_dir / "batch.args").exists() else ""
    smoke_in = out / "inchunks" / "smoke"
    problems = []
    want_ok = case == "clean"
    if (r.returncode == 0) != want_ok:
        problems.append(f"rc={r.returncode}, expected {'0' if want_ok else 'non-zero'}: {(r.stdout + r.stderr)[-300:]}")
    for flag in ("--config configs/c.yaml", "--submit-script " + str(stage / "submit_moirai.sh"), "--src-dir " + str(out / "src"), "--env-dir " + str(case_dir / "venv"),
                 "--prestage-script " + str(out / "src" / "app" / "pre.sh"), "--overwrite", "--cwd " + str(out / "src" / "app"), "--output-dir " + str(out / "app_out" / scale)):
        if flag not in gen:
            problems.append(f"gen_launch_script missing: {flag}")
    if scale == "smoke":
        files = sorted(smoke_in.glob("*")) if smoke_in.exists() else []
        n = len(files[0].read_text().splitlines()) if files else -1
        if len(files) != 1 or files[0].name != "chunk_000000.csv" or n != 2:
            problems.append(f"smoke input wrong: {[f.name for f in files]} lines={n}")
        if "--inchunks " + str(smoke_in) not in gen:
            problems.append("smoke does not pass its own chunk dir")
    elif "--inchunks " + str(splits) not in gen:
        problems.append("baseline does not pass the full split")
    if "-t 1h" not in batch or "--queue pdebug" not in batch:
        problems.append(f"flux batch args: {batch.strip()}")
    return {"case": f"{scale}/{case}", "problems": problems}


def _selftest():
    results, texts = [], {}
    with tempfile.TemporaryDirectory() as t:
        splits = Path(t) / "splits"
        splits.mkdir()
        (splits / "chunk_000000.csv").write_text("MGYP001796749278,,,134\nMGYP001796749279,,,135\nMGYP001796749280,,,136\n")
        (splits / "chunk_000001.csv").write_text("MGYP001796749281,,,137\n")
        for scale in ("smoke", "baseline"):
            for case in ("clean", "exit_code", "failed_line", "no_hosts"):
                results.append(_selftest_case(t, scale, case))
        for name, text, bad in (("exit_code", "flux-job: task(s) exited with exit code 2\n", True),
                                ("failed_line", "Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed\n", True),
                                ("clean", "Fri Oct 30 23:40:00 PDT 2026: chunk_000000 finished\nexit code 0 recorded\n", False),
                                ("empty", "", True)):
            failed, msg = _scan(text, t)
            results.append({"case": f"scan/{name}", "problems": [] if failed == bad else [f"scan said failed={failed}: {msg}"]})
        texts.update(BATCH_STEPS)
        texts["submit_moirai.sh.in"] = WRAPPER_T
        texts["lib"] = LIB
        user = os.environ.get("USER", "")
        forbidden = [x for x in (t, os.path.expanduser("~"), f"/{user}/" if user else "") if x and x != "/"]
        found = [f"{k}: {x}" for k, v in texts.items() for x in forbidden if x in v]
        found += [f"{k}: looks like a job id" for k, v in texts.items() if k != "lib" and JOBID.search(v)]
        if "moirai-service" in BATCH_STEPS["04_run.sh"]:
            found.append("04_run.sh mentions moirai-service: no service belongs on the login node")
        results.append({"case": "portable", "problems": found})
        wrapper = WRAPPER_T
        for tok, val in (("@ORIG@", "/o/orig.sh"), ("@ENVF@", "/o/env.sh"), ("@STAGE@", "/o/st"), ("@TRACES@", "/o/tr"), ("@DATA@", "/o/src"), ("@SHM_BASE@", "/moirai-repro-smoke"), ("@MOIRAI_VENV@", "/o/mv")):
            wrapper = wrapper.replace(tok, val)
        r = subprocess.run(["bash", "-n", "/dev/stdin"], input=wrapper, capture_output=True, text=True)
        results.append({"case": "wrapper", "problems": ([f"bash -n: {r.stderr[:200]}"] if r.returncode else []) + (["unreplaced token in wrapper"] if re.search(r"@[A-Z_]+@", wrapper) else [])})
    bad = [r for r in results if r["problems"]]
    return {"ok": not bad, "verdict": "batch selftest ok" if not bad else "batch selftest FAILED", "cases": len(results), "failed": bad}


def register(mcp):
    @mcp.tool()
    def repro_build(pipeline: str, output_root: str = "${HOME}/moirai_repro/${APP}", mode: str = "") -> dict:
        """Generate final_provenance/ in the session: config.ini, scripts, the provenance patch, the spec, pinned versions and a standalone checker. Use after the query stage, or again after
        a fix: it starts over. output_root: where a reader's run writes; keep ${USER} or ${HOME}, never a literal name. mode: '' detects it (batch when the moirai_smoke or baseline outputs
        record batch_script or submit_command), 'batch' for an app launched by a generated flux batch run.sh (the wrapper starts moirai-service on the worker node, 04_run submits and waits,
        no service on the login node), 'generic' for an app that runs in the shell. Returns {status, package, mode, files, source, pins, warnings}."""
        c = P.load(pipeline)
        integ, ann, base = P.stage(c, "integrate")["outputs"], P.stage(c, "annotate")["outputs"], P.stage(c, "baseline")["outputs"]
        smoke_run = P.stage(c, "moirai_smoke")["outputs"]
        if not ann.get("spec") or not smoke_run.get("traces"):
            return {"status": "error", "error": "needs the annotate and moirai_smoke stages"}
        if mode not in ("", "batch", "generic"):
            return {"status": "error", "error": "mode is '', 'batch' or 'generic'"}
        batch = mode == "batch" or (mode == "" and any(k in o for o in (smoke_run, base) for k in ("batch_script", "submit_command")))
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
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                warnings.append(f"APP_REF is not a full 40-character SHA: {ref!r}")
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
            text = " ".join(str(x) for o in (base, smoke_run) for x in (o.get("command"), o.get("submit_command")) if x) or c["command"]
            cfg = [f"APP={app}" if x.startswith("APP=") else x for x in _batch_cfg(output_root, text, url, ref, pins)]
            if not any(x.startswith("SPLITS_DIR=") and len(x) > 11 for x in cfg):
                warnings.append("SPLITS_DIR not found in the recorded command: set it in config.ini")
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
        if batch:
            _write_batch_scripts(pkg, head)
        (pkg / "scripts" / "run_all.sh").write_text(head + RUN_ALL)
        for f in (pkg / "scripts").iterdir():
            f.chmod(0o755)
        counts = expected.get("baseline") or expected["smoke"]
        extra = ("\n" + BATCH_README) if batch else ""
        (pkg / "README.md").write_text(f"""# {c['name']}: provenance reproduction package

Reproduces the moirai provenance of {c['name']}: {counts['activities']} activities and {counts['entities']} entities in the original run.
Scientific artifacts: {', '.join(ann['spec'].get('scientific_artifacts', []))}.
Mode: {'batch (flux batch run.sh; moirai-service runs on the worker nodes)' if batch else 'generic'}.

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
| `03_build.sh` | {'moirai_env.sh, the shim and the per-chunk wrapper' if batch else 'the patched app against moirai'} |
| `04_run.sh` | {'generate run.sh, flux batch, flux watch, judge the output' if batch else 'the run under `moirai-service`'} |
| `05_validate.sh` | the provenance check against `inputs/expected.json` |
{extra}""")
        files = sorted(str(f.relative_to(pkg)) for f in pkg.rglob("*") if f.is_file())
        P.record(pipeline, STAGE, status="running", outputs={"package": str(pkg), "mode": "batch" if batch else "generic", "files": len(files), "pins": pins, "warnings": warnings, "verified": None, "summary": f"{len(files)} files in final_provenance"}, note="package built")
        return {"status": "ok", "package": str(pkg), "mode": "batch" if batch else "generic", "files": files, "source": url or "bundled", "pins": pins, "warnings": warnings}

    @mcp.tool()
    def repro_verify(pipeline: str, isolation_root: str, scale: str = "smoke", reuse_venv: str = "", limit: int = 3600, wait: int = 300, reuse_moirai_venv: str = "") -> dict:
        """Prove the package works: copy it to a new folder outside the session and run it in a clean login shell, with nothing inherited (env -i, bash -l). Use after repro_build. isolation_root:
        a parent folder outside the session, on a file system a compute node can see. scale: smoke or baseline. reuse_venv: a venv to reuse instead of building one (batch mode: the APP venv).
        reuse_moirai_venv: batch mode only, the moirai venv to reuse; give both or neither. limit: seconds for the whole run, which waits on the batch job (about 8 minutes in batch mode).
        wait: seconds this call waits. Returns {status, job, iso_dir, mode, log_tail}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        if not o.get("package"):
            return {"status": "error", "error": "run repro_build first"}
        batch = _is_batch(o["package"])
        if batch and bool(reuse_venv) != bool(reuse_moirai_venv):
            return {"status": "error", "error": "batch mode reuses both venvs or neither: give reuse_venv (app) and reuse_moirai_venv"}
        root = Path(os.path.expanduser(isolation_root)).resolve()
        if str(root).startswith(str(P.work(c).parent.resolve()) + os.sep):
            return {"status": "error", "error": "isolation_root must be outside the session workspace"}
        iso = root / f"isolation_{time.strftime('%Y%m%d_%H%M%S')}"
        iso.mkdir(parents=True)
        shutil.copytree(o["package"], iso / "pkg", symlinks=True)
        env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
        env.update(TERM="dumb", OUTPUT_ROOT=str(iso / "out"), SCALE=scale)
        if reuse_venv:
            env.update(REUSE_VENV="1", VENV=reuse_venv)
        if batch and reuse_moirai_venv:
            env.update(MOIRAI_VENV=reuse_moirai_venv)
        cmd = f"cd {shlex.quote(str(iso))} && bash pkg/scripts/run_all.sh"
        args = " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
        j = J.start(pipeline, STAGE, "isolated-run", f"env -i {args} bash -lc {shlex.quote(cmd)}\n", str(iso), limit=limit)
        P.record(pipeline, STAGE, outputs={"verify": {"job": j["id"], "iso_dir": str(iso), "scale": scale, "mode": "batch" if batch else "generic", "env_build_exercised": not reuse_venv}}, note=f"isolated run of {scale} in {iso}")
        r = J.wait(pipeline, j["id"], wait)
        return {"status": r["status"], "job": j["id"], "rc": r["rc"], "iso_dir": str(iso), "mode": "batch" if batch else "generic", "log_tail": r.get("log_tail", "")[-2500:]}

    @mcp.tool()
    def repro_batch_scan(run_out: str) -> dict:
        """Run the package's own batch-output failure scan (the bash scan_run_out of 04_run.sh) on the text of a flux batch --output file. Use to check what a batch run.out means, or to test the
        patterns. run_out: the file text. Returns {verdict: 'CLEAN' or 'FAILED', reasons}."""
        with tempfile.TemporaryDirectory() as t:
            failed, msg = _scan(run_out, t)
        return {"verdict": "FAILED" if failed else "CLEAN", "reasons": msg.splitlines()}

    @mcp.tool()
    def repro_batch_selftest() -> dict:
        """Sandbox test of the batch package on pure renders: runs the real 04_run.sh (smoke and baseline) with a stub flux, module and app python against clean and failing outputs
        (exit code 2, a ' failed' line, no hosts.txt), checks the generated run.sh flags, the 2-line smoke input, the failure scan, that no literal user, home, session or tool path is in any
        script and that the wrapper renders and parses. It starts nothing on a cluster. Returns {ok, verdict, cases, failed}."""
        return _selftest()

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
        v.check("isolated_run", j["rc"] == 0, f"rc={j['rc']}: {j.get('log_tail', '')[-300:]}", "read the log: something the session provided is missing from the package")
        run = Path(vf["iso_dir"]) / "out" / "run.json"
        if v.check("reproduced", run.exists() and json.loads(run.read_text()).get("ok"), run.read_text()[:300] if run.exists() else "no run.json", "the counts differ: is the app deterministic? Is the input the same?"):
            note = "" if vf.get("env_build_exercised", True) else " (venvs reused: the environment build was not exercised)"
            P.record(pipeline, STAGE, outputs={"summary": f"reproduced in a clean shell: {json.loads(run.read_text())['counts']}{note}"})
        return P.finish_validation(pipeline, v)
