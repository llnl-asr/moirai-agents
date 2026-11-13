"""Background jobs of a pipeline: detached processes with a log and an exit-code file, so they outlive the MCP call
(and a refresh of the MCP server or web UI). The Jobs panel and the capabilities read the same files."""
from __future__ import annotations

import json
import os
import re
import signal
import shutil
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


# ---- the scheduler: Flux or Slurm, whichever answers on this system (both, if both do). MOIRAI_AGENTS_SCHEDULER=flux|slurm|none pins it.
SCHED_COUNT, SCHED_TIMEOUT_S, SCHED_CACHE_S = 100, 8, 10
FLUX_COUNT, FLUX_TIMEOUT_S, FLUX_CACHE_S = SCHED_COUNT, SCHED_TIMEOUT_S, SCHED_CACHE_S
FLUX_FORMAT = "{id.f58}|{state}|{result}|{name}|{queue}|{nnodes}|{t_submit}|{runtime}|{t_run}|{t_inactive}"
FLUX_WAITING = ("DEPEND", "PRIORITY", "SCHED")
FLUX_ID = re.compile(r"^[A-Za-z0-9]{2,32}$")
FLUX_REDIRECT = re.compile(r"(?:stdout|stderr) redirected to (\S+)")
FLUX_ATTACH_S, OUTPUT_CHARS = 5, 20000
SLURM_FORMAT = "JobID,State,JobName,Partition,NNodes,Submit,Elapsed,Start,End"
SLURM_SQUEUE = "%i|%T|%j|%P|%D|%V|%M|%S"
SLURM_WAITING = ("PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD")
SLURM_RUNNING = ("RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "STAGE_OUT", "SIGNALING", "RESIZING", "STOPPED")
SLURM_ID = re.compile(r"^[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?$")
SCHEDULERS = ("flux", "slurm")
_sched_cache: Dict[str, Any] = {"at": 0.0, "since": None, "rows": [], "available": []}
_flux_cache = _sched_cache


def _flux_row(line: str) -> Optional[Dict[str, Any]]:
    """One line of FLUX_FORMAT, with the group it belongs in: scheduled, running, passed or failed."""
    f = line.strip().split("|")
    if len(f) != 10:
        return None
    jid, st, result, name, queue, nodes, submitted, runtime, started, ended = f
    group = "sched" if st in FLUX_WAITING else "run" if st != "INACTIVE" else "ok" if result == "COMPLETED" else "bad"
    try:
        return {"id": jid, "sched": "flux", "state": st, "result": result, "name": name, "queue": queue, "nodes": nodes, "submitted": float(submitted),
                "runtime": float(runtime or 0), "group": group, "updated": max(float(submitted), float(started or 0), float(ended or 0))}
    except ValueError:
        return None


def _slurm_time(s: str) -> float:
    """A Slurm timestamp (local time, 2026-11-12T15:05:34) as epoch seconds; 0 for Unknown/None/N/A."""
    try:
        return time.mktime(time.strptime(s.strip(), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def _slurm_elapsed(s: str) -> float:
    """[D-][HH:]MM:SS as seconds."""
    days, _, hms = s.strip().rpartition("-")
    try:
        parts = [float(x) for x in hms.split(":")]
        sec = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
        return sec + (float(days) * 86400 if days else 0)
    except ValueError:
        return 0.0


def _slurm_group(st: str) -> str:
    return "sched" if st in SLURM_WAITING else "run" if st in SLURM_RUNNING else "ok" if st == "COMPLETED" else "bad"


def _slurm_row(line: str, squeue: bool = False) -> Optional[Dict[str, Any]]:
    """One line of sacct (SLURM_FORMAT) or squeue (SLURM_SQUEUE): id|state|name|partition|nodes|submit|elapsed|start[|end]."""
    f = line.strip().split("|")
    if len(f) < (8 if squeue else 9) or not f[0]:
        return None
    jid, st_full, name, queue, nodes, submit, elapsed, start = f[:8]
    end = "" if squeue else f[8]
    st = (st_full.split() or [""])[0].rstrip("+")          # "CANCELLED by 1234", "OUT_OF_MEMORY"
    sub = _slurm_time(submit)
    return {"id": jid, "sched": "slurm", "state": st, "result": st_full.strip() if _slurm_group(st) in ("ok", "bad") else "", "name": name,
            "queue": queue, "nodes": nodes, "submitted": sub, "runtime": _slurm_elapsed(elapsed), "group": _slurm_group(st),
            "updated": max(sub, _slurm_time(start), _slurm_time(end))}


def _run(cmd: List[str], timeout: float = SCHED_TIMEOUT_S) -> Optional[str]:
    """stdout of a scheduler command, or None when the command is missing, fails or hangs (the scheduler is not usable here)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _flux_jobs(since: float) -> Optional[List[Dict[str, Any]]]:
    out = _run(["flux", "jobs", "-a", "-n", f"--count={SCHED_COUNT}", "-o", FLUX_FORMAT])
    return None if out is None else [x for x in map(_flux_row, out.splitlines()) if x]


def _slurm_jobs(since: float) -> Optional[List[Dict[str, Any]]]:
    """sacct (finished jobs too) where accounting is on; squeue (queued and running only) where it is not."""
    user = os.environ.get("USER") or ""
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since or time.time() - 86400))
    out = _run(["sacct", "-X", "-n", "-P", "-u", user, "-S", start, f"--format={SLURM_FORMAT}"])
    if out is not None:
        return [x for x in map(_slurm_row, out.splitlines()) if x][-SCHED_COUNT:]
    out = _run(["squeue", "-h", "-u", user, "-o", SLURM_SQUEUE])
    return None if out is None else [x for x in (_slurm_row(ln, squeue=True) for ln in out.splitlines()) if x]


def schedulers() -> List[str]:
    """The schedulers this system may have, in the order they are asked: MOIRAI_AGENTS_SCHEDULER, else every one whose client is installed."""
    pin = os.environ.get("MOIRAI_AGENTS_SCHEDULER", "").strip().lower()
    if pin:
        return [s for s in re.split(r"[,\s]+", pin) if s in SCHEDULERS]
    return [s for s, prog in (("flux", "flux"), ("slurm", "squeue")) if shutil.which(prog)]


def submitted(since: float = 0.0) -> List[Dict[str, Any]]:
    """This user's jobs in every scheduler that answers here (Flux, Slurm), since a time, the latest update first; each row says
    which scheduler (``sched``). Empty where there is none. Read at most once per SCHED_CACHE_S, since the panel polls."""
    return scheduler_view(since)["jobs"]


def scheduler_view(since: float = 0.0) -> Dict[str, Any]:
    """{jobs, schedulers}: the jobs as :func:`submitted`, and the schedulers that answered (an installed client without a running
    service, like flux on a Slurm system, is left out)."""
    now = time.time()
    if now - _sched_cache["at"] >= SCHED_CACHE_S or (_sched_cache["since"] is not None and since < _sched_cache["since"]):
        rows, avail = [], []
        for s in schedulers():
            got = (_flux_jobs if s == "flux" else _slurm_jobs)(since)
            if got is not None:
                avail.append(s)
                rows += got
        _sched_cache.update(at=now, since=since, rows=rows, available=avail)
    jobs = sorted((j for j in _sched_cache["rows"] if j["submitted"] >= since), key=lambda j: -j["updated"])
    return {"jobs": jobs, "schedulers": list(_sched_cache["available"])}


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


def _read_outputs(paths: List[str]) -> str:
    """What a job wrote to its output files: the text alone when there is one file (stdout and stderr in the same file count
    once), each under a ``--- path ---`` line when there are several, and a ``--- path: reason ---`` line for one that cannot be read.
    Flux and Slurm jobs show their output the same way through this."""
    paths = list(dict.fromkeys(p for p in paths if p))
    out = ""
    for path in paths:
        try:
            text = Path(path).read_text(errors="replace")[-OUTPUT_CHARS:]
            out += (f"--- {path} ---\n" if len(paths) > 1 else "") + text + ("" if text.endswith("\n") or not text else "\n")
        except OSError as e:
            out += f"--- {path}: {e.strerror} ---\n"
    return out


def _redirected(attached: str) -> str:
    """A Flux batch job sends its output to a file and attach only says so ("stdout redirected to X"): the file's text instead
    of those lines, with whatever else attach printed before it."""
    files = FLUX_REDIRECT.findall(attached)
    rest = "\n".join(ln for ln in attached.splitlines() if not FLUX_REDIRECT.search(ln)).strip("\n")
    return (rest + "\n" if rest else "") + _read_outputs(files)


def sched_detail(sched: str, jid: str) -> Dict[str, Any]:
    """What a scheduler job runs, where, and what it wrote so far, from Flux or Slurm."""
    if sched == "slurm":
        return slurm_detail(jid)
    if sched in ("", "flux"):
        return flux_detail(jid)
    return {"status": "error", "error": f"unknown scheduler {sched}"}


def _kv(text: str) -> Dict[str, str]:
    """scontrol -o: Key=Value pairs on one line; a value may hold spaces (Command=, Comment=) up to the next Key=."""
    out, key = {}, None
    for tok in text.strip().split(" "):
        k, eq, v = tok.partition("=")
        if eq and re.fullmatch(r"[A-Za-z][A-Za-z0-9_:/]*", k):
            key, out[k] = k, v
        elif key:
            out[key] += " " + tok
    return out


def _slurm_path(pattern: str, jid: str, name: str, workdir: str) -> str:
    """A Slurm output-file pattern (%j, %A, %a, %x, %u, %%) made concrete, relative to the job's working directory."""
    if not pattern:
        return ""
    base, _, task = jid.partition("_")
    subs = {"j": jid.split("+")[0], "A": base, "a": task or "", "x": name, "u": os.environ.get("USER", ""), "%": "%"}
    path = re.sub(r"%(\d*)([jAaxu%])", lambda m: subs[m.group(2)].zfill(int(m.group(1) or 0)), pattern)
    return path if os.path.isabs(path) or not workdir else os.path.join(workdir, path)


def _script_from(submit: str, workdir: str) -> str:
    """The script an sbatch line ran, read from disk, for a job whose script Slurm did not keep. It may have changed since, and says so."""
    for tok in submit.split()[1:]:
        f = Path(tok if os.path.isabs(tok) else os.path.join(workdir or ".", tok))
        try:
            if not tok.startswith("-") and f.is_file() and f.stat().st_size < 1_000_000 and f.read_bytes()[:2] == b"#!":
                text = f.read_text(errors="replace")
                lines = text.splitlines(keepends=True)
                return lines[0] + f"# {f}, as it is on disk now (Slurm kept no copy); submitted as: {submit}\n" + "".join(lines[1:])
        except OSError:
            continue
    return ""


def _sacct_script(text: str) -> str:
    """sacct --batch-script without its banner; empty when Slurm kept none (an srun job, or scripts not stored)."""
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("Batch Script for") and not re.fullmatch(r"-{10,}", ln))
    return "" if body.strip() in ("", "NONE") else body.strip("\n") + "\n"


def slurm_detail(jid: str) -> Dict[str, Any]:
    """A Slurm job: scontrol while the controller remembers it (queued, running, just ended), sacct after. Output comes from
    the job's StdOut/StdErr files (default slurm-<id>.out in its working directory); an srun job without a file has none to show."""
    if not SLURM_ID.match(jid or ""):
        return {"status": "error", "error": "bad job id"}
    info = _kv(_run(["scontrol", "show", "job", "-o", jid]) or "")
    name = info.get("JobName", "")
    if info:
        workdir, command = info.get("WorkDir", ""), info.get("Command", "")
        batch = info.get("BatchFlag") == "1" and _run(["scontrol", "write", "batch_script", jid, "-"])
        outs = [info.get("StdOut", ""), info.get("StdErr", "")]
    else:
        row = (_run(["sacct", "-X", "-n", "-P", "-j", jid, "--format=WorkDir,SubmitLine,JobName,StdOut,StdErr"]) or "").strip().splitlines()
        if not row:      # an older sacct without SubmitLine/StdOut
            row = (_run(["sacct", "-X", "-n", "-P", "-j", jid, "--format=WorkDir,JobName"]) or "").strip().splitlines()
            row = [row[0] + "|||" if row else ""]
        f = (row[0] if row else "").split("|") + [""] * 5
        workdir, command, name = f[0], f[1], f[2]
        batch = _sacct_script(_run(["sacct", "-j", jid, "--batch-script"]) or "") or (command.startswith("sbatch") and _script_from(command, workdir))
        outs = [_slurm_path(f[3], jid, name, workdir), _slurm_path(f[4], jid, name, workdir)]
        if not (outs[0] or outs[1]) and not command.startswith("srun"):
            outs = [os.path.join(workdir, f"slurm-{jid.split('+')[0]}.out")] if workdir else []
    if not (info or workdir or command):
        return {"status": "error", "error": f"Slurm does not know job {jid} (sacct and scontrol have nothing on it)"}
    files = list(dict.fromkeys(o for o in outs if o))
    output = _read_outputs(files)[-OUTPUT_CHARS:] if files else "(no output file: an srun job writes to the terminal that started it)"
    limit = info.get("TimeLimit", "")
    return {"id": jid, "sched": "slurm", "command": batch or command, "cwd": workdir, "files": files,
            "duration": _slurm_elapsed(limit) if limit[:1].isdigit() else None, "output": output}


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
    attached = _flux_text(["job", "attach", "--read-only", jid], FLUX_ATTACH_S)
    output = _redirected(attached)[-OUTPUT_CHARS:]
    batch = ((sysattr.get("files") or {}).get("script") or {}).get("data")
    return {"id": jid, "sched": "flux", "command": batch if isinstance(batch, str) and batch else " ".join(map(str, tasks[0].get("command") or [])),
            "cwd": sysattr.get("cwd", ""), "files": list(dict.fromkeys(FLUX_REDIRECT.findall(attached))), "duration": sysattr.get("duration"), "output": output}

