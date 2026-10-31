"""Track and clean up everything a web UI server starts.

Every server run gets an instance id, exported as MOIRAI_AGENTS_WEBUI_INSTANCE; all its children inherit it
(claude turns, their MCP servers, Bash commands, anything they start, even after setsid/nohup), so a
process can be traced back to its server by its environment. Each server writes a state file
<RUN_DIR>/<host>-<pid>.json (port, URL, log, instance, running turns) and removes it on clean exit.

    python -m moirai_agents.webui.procs status            # servers + their processes on this host
    python -m moirai_agents.webui.procs stop [--grace S]  # stop servers on this host, then sweep leftovers
    python -m moirai_agents.webui.procs sweep             # kill processes of servers that no longer run

Only processes of the current Unix user are ever touched. Flux jobs already submitted to the queue are
not local processes and are left alone (evidence runs keep going and are ingested later).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[3]
RUN_DIR = Path(os.environ.get("MOIRAI_AGENTS_WEBUI_RUNDIR", REPO / "logs" / "webui"))
TAG = "MOIRAI_AGENTS_WEBUI_INSTANCE"
HOST = socket.gethostname().split(".")[0]


# ------------------------------------------------------------------ state files
class Registry:
    """The running server's state file (written atomically, removed on clean exit)."""

    def __init__(self, data: dict) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RUN_DIR / f"{HOST}-{os.getpid()}.json"
        self.data = dict(data, host=HOST, pid=os.getpid(), pgid=os.getpgid(0), started=time.time(), turns={})
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    def turn_started(self, pid: int, session: str, user: str) -> None:
        self.data["turns"][str(pid)] = {"session": session, "user": user, "started": time.time()}
        self.save()

    def turn_ended(self, pid: int) -> None:
        if self.data["turns"].pop(str(pid), None) is not None:
            self.save()

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def servers(host: Optional[str] = HOST) -> List[dict]:
    out = []
    for f in sorted(RUN_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if host and d.get("host") != host:
            continue
        d["file"] = str(f)
        d["alive"] = d.get("host") == HOST and _server_alive(d.get("pid"))
        out.append(d)
    return out


# ------------------------------------------------------------------ processes
def _alive(pid, instance=None) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return instance is None or _instance_of(int(pid)) == instance


def _server_alive(pid) -> bool:
    """The server sets its tag after it started, so its own /proc environ lacks it: match the command."""
    return _alive(pid) and "moirai_agents.webui.server" in _cmd(int(pid))


def _instance_of(pid: int) -> Optional[str]:
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for kv in env.split(b"\0"):
        if kv.startswith(TAG.encode() + b"="):
            return kv.split(b"=", 1)[1].decode(errors="replace")
    return None


def _cmd(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def tagged(instance: Optional[str] = None) -> Dict[int, str]:
    """pid -> instance for my processes started by a web UI server (or by one given instance)."""
    me, uid, out = os.getpid(), os.getuid(), {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if d.stat().st_uid != uid:
                continue
        except OSError:
            continue
        inst = _instance_of(int(d.name))
        if inst and (instance is None or inst == instance):
            out[int(d.name)] = inst
    return out


def terminate(pids, grace: float = 5.0) -> dict:
    """SIGTERM (whole process groups where possible), wait `grace` s, then SIGKILL what is left."""
    pids = [p for p in set(pids) if p != os.getpid()]
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + grace
    while time.time() < end and any(_alive(p) for p in pids):
        time.sleep(0.2)
    killed = []
    for p in pids:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
                killed.append(p)
            except OSError:
                pass
    return {"terminated": len(pids) - len(killed), "killed": killed}


def sweep(instance: Optional[str] = None, grace: float = 3.0, keep_live_servers: bool = True) -> dict:
    """Kill tagged processes of `instance`, or (None) of every server that is no longer running."""
    procs = tagged(instance)
    if instance is None and keep_live_servers:
        live = {d["instance"] for d in servers(HOST) if d["alive"]}
        procs = {p: i for p, i in procs.items() if i not in live}
    info = {p: _cmd(p)[:160] for p in procs}
    r = terminate(list(procs), grace) if procs else {"terminated": 0, "killed": []}
    return dict(r, processes=info)


def stop(grace: float = 30.0) -> List[dict]:
    """Stop every web UI server of mine on this host: graceful SIGTERM (the server stops its turns and
    their processes), SIGKILL after `grace` s, then sweep anything still tagged with its instance."""
    out = []
    for d in servers(HOST):
        rec = {"pid": d.get("pid"), "port": d.get("port"), "log": d.get("log")}
        if d["alive"]:
            os.kill(int(d["pid"]), signal.SIGTERM)
            end = time.time() + grace
            while time.time() < end and _server_alive(d["pid"]):
                time.sleep(0.25)
            if _server_alive(d["pid"]):
                os.kill(int(d["pid"]), signal.SIGKILL)
                rec["forced"] = True
        rec["leftovers"] = sweep(d.get("instance"), grace=3.0) if d.get("instance") else {}
        Path(d["file"]).unlink(missing_ok=True)
        out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "stop", "sweep"])
    ap.add_argument("--grace", type=float, default=30.0, help="stop: seconds before SIGKILL")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        res = []
        for d in servers(HOST):
            kids = tagged(d.get("instance")) if d["alive"] else {}
            res.append({k: d.get(k) for k in ("pid", "port", "url", "log", "alive", "instance")}
                       | {"turns": d.get("turns", {}), "processes": {p: _cmd(p)[:120] for p in kids}})
        orphans = sweep_preview()
        if a.json:
            print(json.dumps({"host": HOST, "servers": res, "orphans": orphans}, indent=1))
        else:
            if not res:
                print(f"{HOST}: no web UI server")
            for r in res:
                state = "running" if r["alive"] else "NOT running (stale state file)"
                print(f"{HOST}: server pid {r['pid']} {state}  {r.get('url') or ''}\n  log: {r['log']}")
                for pid, t in r["turns"].items():
                    print(f"  turn  pid {pid}: user {t['user']}, chat {t['session']}, "
                          f"{(time.time() - t['started']) / 60:.0f} min")
                for pid, c in r["processes"].items():
                    print(f"  proc  {pid}: {c}")
            for pid, c in orphans.items():
                print(f"  ORPHAN {pid} (its server is gone): {c}")
        return 0
    if a.cmd == "stop":
        res = stop(a.grace)
        orphans = sweep()
        if a.json:
            print(json.dumps({"host": HOST, "stopped": res, "orphans": orphans}, indent=1))
        else:
            if not res:
                print(f"{HOST}: no web UI server running")
            for r in res:
                lo = r.get("leftovers") or {}
                print(f"{HOST}: stopped server pid {r['pid']} (port {r['port']})"
                      f"{' with SIGKILL' if r.get('forced') else ''}; cleaned {len(lo.get('processes', {}))} "
                      f"leftover process(es); log: {r['log']}")
            if orphans["processes"]:
                print(f"{HOST}: cleaned {len(orphans['processes'])} orphan process(es) of earlier servers")
        return 0
    r = sweep()
    print(json.dumps(r, indent=1) if a.json else f"{HOST}: cleaned {len(r['processes'])} orphan process(es)")
    return 0


def sweep_preview() -> Dict[int, str]:
    live = {d["instance"] for d in servers(HOST) if d["alive"]}
    return {p: _cmd(p)[:120] for p, i in tagged().items() if i not in live}


if __name__ == "__main__":
    sys.exit(main())
