"""Background jobs of a pipeline: detached processes with a log and an exit-code file, so they outlive the MCP call
(and a refresh of the MCP server or web UI). The Jobs panel and the capabilities read the same files."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import pipeline as P


def _jdir(pid: str, workspace: Optional[str] = None) -> Path:
    return P.pdir(pid, workspace) / "jobs"


DEFAULT_LIMIT = int(os.environ.get("MOIRAI_AGENTS_JOB_LIMIT_S", "3600"))


def start(pid: str, stage: str, name: str, script: str, cwd: str, env: Optional[Dict[str, str]] = None,
          limit: Optional[int] = None) -> Dict[str, Any]:
    """Run ``script`` (bash) detached, for at most ``limit`` seconds: nothing stays behind on a login node. On expiry it
    gets SIGTERM (so its traps run: moirai-service is stopped), then SIGKILL 30 s later; exit code 124 means it timed out."""
    limit = int(limit or DEFAULT_LIMIT)
    d = _jdir(pid)
    d.mkdir(parents=True, exist_ok=True)
    jid = f"{re.sub(r'[^a-z0-9_]+', '-', name.lower())}-{int(time.time() * 10) % 10**9}"
    sh, log, rc = d / f"{jid}.sh", d / f"{jid}.log", d / f"{jid}.rc"
    sh.write_text(script)
    runner = f'timeout --signal=TERM --kill-after=30 {limit} bash {sh} > {log} 2>&1; echo $? > {rc}'
    p = subprocess.Popen(["bash", "-c", runner], cwd=cwd, env={**os.environ, **(env or {})}, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec = {"id": jid, "pipeline": pid, "stage": stage, "name": name, "pid": p.pid, "cwd": cwd, "started": round(time.time(), 1), "limit_s": limit,
           "script": str(sh), "log": str(log), "rc_file": str(rc)}
    (d / f"{jid}.json").write_text(json.dumps(rec, indent=1))
    return rec


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


def state(rec: Dict[str, Any], tail: int = 0) -> Dict[str, Any]:
    rc_f = Path(rec["rc_file"])
    out = dict(rec)
    if rc_f.exists() and rc_f.read_text().strip():
        out["status"], out["rc"] = "done", int(rc_f.read_text().strip())
        out["timed_out"] = out["rc"] in (124, 137)
        out["ended"] = round(rc_f.stat().st_mtime, 1)
    elif _alive(rec["pid"]):
        out["status"], out["rc"] = "running", None
    else:
        out["status"], out["rc"] = "lost", None
    out["elapsed"] = round(out.get("ended", time.time()) - rec["started"], 1)
    if tail:
        try:
            with open(rec["log"], "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - tail))
                out["log_tail"] = fh.read().decode("utf-8", "replace")
        except OSError:
            out["log_tail"] = ""
    return out


def get(pid: str, jid: str, tail: int = 4000, workspace: Optional[str] = None) -> Dict[str, Any]:
    p = _jdir(pid, workspace) / f"{jid}.json"
    if not p.exists():
        raise P.PipelineError(f"no job {jid} in {pid}")
    return state(json.loads(p.read_text()), tail)


def list_jobs(pid: str, workspace: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _jdir(pid, workspace)
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out.append(state(json.loads(p.read_text())))
        except (ValueError, KeyError, OSError):
            continue
    return sorted(out, key=lambda j: -j["started"])


def wait(pid: str, jid: str, timeout: float) -> Dict[str, Any]:
    """Wait up to ``timeout`` seconds; a job still running then is returned as running (it keeps going)."""
    end = time.time() + timeout
    while True:
        j = get(pid, jid)
        if j["status"] != "running" or time.time() >= end:
            return j
        time.sleep(0.5)


def stop(pid: str, jid: str) -> Dict[str, Any]:
    j = get(pid, jid, 0)
    if j["status"] == "running":
        os.killpg(j["pid"], signal.SIGTERM)
    return {"status": "ok", "id": jid, "stopped": j["status"] == "running"}


FLUX_FORMAT = "{id.f58}|{state}|{result}|{name}|{queue}|{nnodes}|{t_submit}|{runtime}|{t_run}|{t_inactive}"
FLUX_COUNT, FLUX_TIMEOUT_S, FLUX_CACHE_S = 100, 8, 10
FLUX_WAITING = ("DEPEND", "PRIORITY", "SCHED")
FLUX_ID = re.compile(r"^[A-Za-z0-9]{2,32}$")
FLUX_REDIRECT = re.compile(r"(?:stdout|stderr) redirected to (\S+)")
FLUX_ATTACH_S, OUTPUT_CHARS = 5, 20000
_flux_cache: Dict[str, Any] = {"at": 0.0, "rows": []}


def _flux_row(line: str) -> Optional[Dict[str, Any]]:
    """One line of FLUX_FORMAT, with the group it belongs in: scheduled, running, passed or failed."""
    f = line.strip().split("|")
    if len(f) != 10:
        return None
    jid, st, result, name, queue, nodes, submitted, runtime, started, ended = f
    group = "sched" if st in FLUX_WAITING else "run" if st != "INACTIVE" else "ok" if result == "COMPLETED" else "bad"
    try:
        return {"id": jid, "state": st, "result": result, "name": name, "queue": queue, "nodes": nodes, "submitted": float(submitted), "runtime": float(runtime or 0), "group": group,
                "updated": max(float(submitted), float(started or 0), float(ended or 0))}
    except ValueError:
        return None


def submitted(since: float = 0.0) -> List[Dict[str, Any]]:
    """This user's jobs in the scheduler since a time, the latest update first. Empty where there is no flux. Read at most once per FLUX_CACHE_S, since the panel polls."""
    now = time.time()
    if now - _flux_cache["at"] >= FLUX_CACHE_S:
        _flux_cache["at"] = now
        try:
            r = subprocess.run(["flux", "jobs", "-a", "-n", f"--count={FLUX_COUNT}", "-o", FLUX_FORMAT], capture_output=True, text=True, timeout=FLUX_TIMEOUT_S)
            _flux_cache["rows"] = [x for x in map(_flux_row, r.stdout.splitlines()) if x] if r.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired):
            _flux_cache["rows"] = []
    return sorted((j for j in _flux_cache["rows"] if j["submitted"] >= since), key=lambda j: -j["updated"])


def _command(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")[-OUTPUT_CHARS:]
    except OSError:
        return ""


def detail(pid: str, jid: str, workspace: Optional[str] = None) -> Dict[str, Any]:
    """What a process ran and what it wrote so far."""
    j = state(json.loads((_jdir(pid, workspace) / f"{jid}.json").read_text()))
    try:
        output = Path(j["log"]).read_text(errors="replace")[-OUTPUT_CHARS:]
    except OSError:
        output = ""
    return {"id": jid, "command": _command(j["script"]), "output": output, "cwd": j["cwd"], "log": j["log"], "status": j["status"], "rc": j["rc"], "running": j["status"] == "running"}


def _flux_text(args: List[str], timeout: float) -> str:
    """A flux command's output; what it wrote so far if it is still going when the time is up, as attach is for a running job."""
    try:
        r = subprocess.run(["flux", *args], capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        return ((e.stdout or b"") + (e.stderr or b"")).decode(errors="replace")
    except OSError:
        return ""


def _redirected(attached: str) -> str:
    """A batch job sends its output to a file and attach only says so: show that file too, as far as it can be read."""
    out = attached
    for path in dict.fromkeys(FLUX_REDIRECT.findall(attached)):
        try:
            out += f"\n--- {path} ---\n" + Path(path).read_text(errors="replace")[-OUTPUT_CHARS:]
        except OSError as e:
            out += f"\n--- {path}: {e.strerror} ---\n"
    return out


def flux_detail(jid: str) -> Dict[str, Any]:
    """What a scheduler job runs and what it wrote so far: output comes from the job's own log in flux."""
    if not FLUX_ID.match(jid or ""):
        return {"status": "error", "error": "bad job id"}
    try:
        spec = json.loads(_flux_text(["job", "info", jid, "jobspec"], FLUX_TIMEOUT_S))
    except ValueError:
        spec = {}
    sysattr = (spec.get("attributes") or {}).get("system") or {}
    tasks = spec.get("tasks") or [{}]
    output = _redirected(_flux_text(["job", "attach", "--read-only", jid], FLUX_ATTACH_S))[-OUTPUT_CHARS:]
    batch = ((sysattr.get("files") or {}).get("script") or {}).get("data")
    return {"id": jid, "command": batch if isinstance(batch, str) and batch else " ".join(map(str, tasks[0].get("command") or [])), "cwd": sysattr.get("cwd", ""),
            "duration": sysattr.get("duration"), "output": output}

