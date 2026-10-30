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
