import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Nothing a test does reaches the real store."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOIRAI_AGENTS_USER_WORKSPACE", str(tmp_path / "uw"))
    monkeypatch.setenv("MOIRAI_AGENTS_NO_SESSIONS", "1")     # a session needs dftracer-agents: test_session.py turns this off
    monkeypatch.setenv("MOIRAI_AGENTS_PIPELINES_DIR", str(tmp_path / "pipelines"))
    monkeypatch.setenv("MOIRAI_AGENTS_LEARNING_DIR", str(tmp_path / "learning"))
    monkeypatch.setenv("MOIRAI_AGENTS_USER_CAPABILITIES", str(tmp_path / "user_caps"))
    monkeypatch.setenv("MOIRAI_AGENTS_PROJECT_CAPABILITIES", str(tmp_path / "project_caps"))
    (tmp_path / "home").mkdir()
    from moirai_agents.core import capstore
    capstore._State.loaded, capstore._State.fingerprint = {}, ""


def tools():
    from moirai_agents.core import capstore
    t = {}
    capstore.refresh(t, force=True)
    return t


@pytest.fixture
def call():
    t = tools()
    return lambda _tool, **kw: t[_tool]["fn"](**kw)


def fake_moirai(root: Path) -> Path:
    """A Python on which `moirai` and `moirai-service` exist. The fake app library records activities; the fake service
    collects them into a node trace of gzip members, the way dftracer_service does, while the app runs."""
    b = root / "bin"
    b.mkdir(parents=True)
    py = b / "python"
    py.write_text(f'#!/bin/sh\nexport PYTHONPATH="{root}/fake:$PYTHONPATH"\nexec {sys.executable} "$@"\n')
    py.chmod(0o755)
    (b / "python3").symlink_to("python")
    lib = root / "fake"
    (lib / "moirai").mkdir(parents=True)
    (lib / "moirai" / "__init__.py").write_text('''
import enum, gzip, json, os, time
class Role(enum.IntEnum):
    UNKNOWN=0; INPUT=1; OUTPUT=2; INTERMEDIATE=3; PARAMETER=4; REFERENCE=5
class Store(enum.IntEnum):
    MEMORY=0; GPU_MEMORY=1; LOCAL_DISK=2; PARALLEL_FS=3
_seen = set()
def _emit(rec):
    with open(os.environ["MOIRAI_LOG_FILE"] + "_node1.pfw.gz", "ab") as fh:
        fh.write(gzip.compress((json.dumps({"pid": os.getpid(), "tid": 1, **rec}) + "\\n").encode()))
def initialize(log=""): return True
def finalize(): pass
def entity_type(name, role, description=""):
    _emit({"name": "ET", "cat": "dftracer", "type": 128, "ph": 4, "args": {"type": name, "role": int(role), "description": description}})
def _entity(t, key, store, uri):
    eid = format(abs(hash((t, key))) & (2**64 - 1), "016x")
    if eid not in _seen:
        _seen.add(eid)
        _emit({"name": "EH", "cat": "dftracer", "type": 128, "ph": 4, "args": {"id": eid, "type": t, "store": int(store), "uri": uri}})
    return eid
class Activity:
    def __init__(self, name): self.name, self.r = name, {}
    def __enter__(self): self.t = time.time(); return self
    def uses(self, t, key, store=Store.MEMORY, uri=""): self.r.setdefault("used", []).append(_entity(t, key, store, uri))
    def generates(self, t, key, store=Store.MEMORY, uri=""): self.r.setdefault("generated", []).append(_entity(t, key, store, uri))
    def __exit__(self, *a):
        _emit({"name": self.name, "cat": "moirai", "type": 128, "ph": 1, "ts": int(self.t * 1e6), "dur": int((time.time() - self.t) * 1e6), "args": {"relations": self.r}})
''')
    for n in ("moirai", "dftracer"):
        d = lib / f"{n}-1.0.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {n}\nVersion: 1.0\n")
    svc = b / "moirai-service"
    svc.write_text('#!/bin/sh\ncase "$1" in start) head -c 100 /dev/zero > "/dev/shm$MOIRAI_SERVICE_SHM";; stop) rm -f "/dev/shm$MOIRAI_SERVICE_SHM";; esac\n')
    svc.chmod(0o755)
    return py


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Web:
    """A real web server from a private copy of the package, so tests can edit its code."""

    def __init__(self, tmp: Path, **env) -> None:
        self.root = tmp / "copy"
        shutil.copytree(REPO / "src", self.root / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        self.port = free_port()
        self.pkg = self.root / "src" / "moirai_agents"
        e = {**os.environ, "PYTHONPATH": str(self.root / "src"), "HOME": str(tmp / "home"), "PYTHONUNBUFFERED": "1",
             "MOIRAI_AGENTS_WEBUI_RUNDIR": str(tmp / "run"), "MOIRAI_AGENTS_WORKSPACE": str(tmp / "ws"), "MOIRAI_AGENTS_WEBUI_STATE": str(tmp / "state"),
             "MOIRAI_AGENTS_WEBUI_TOKEN": "test-token", **env}
        e.pop("MOIRAI_AGENTS_PIPELINES_DIR", None)
        self.log = open(tmp / "web.log", "wb")
        self.logp = tmp / "web.log"
        self.p = subprocess.Popen([sys.executable, "-m", "moirai_agents.webui.server", "--port", str(self.port)], env=e, stdout=self.log, stderr=subprocess.STDOUT, cwd=tmp)
        self.wait(lambda: self.ok())

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, tok=None, timeout=5):
        r = urllib.request.Request(self.url(path), headers={"X-Moirai-Token": tok or "test-token"})
        with urllib.request.urlopen(r, timeout=timeout) as fh:
            return json.loads(fh.read())

    def post(self, path, body, tok=None):
        r = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), headers={"X-Moirai-Token": tok or "test-token", "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=5) as fh:
            return json.loads(fh.read())

    def ok(self):
        try:
            return self.get("/api/version")
        except OSError:
            return None

    @staticmethod
    def wait(fn, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.2)
        raise AssertionError("timed out waiting")

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(25)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.log.close()


@pytest.fixture
def web(tmp_path):
    made = []

    def start(**env):
        w = Web(tmp_path, **env)
        made.append(w)
        return w
    yield start
    for w in made:
        w.stop()


class Mcp:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, "-m", "moirai_agents.mcp_server"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.id, self.buf = 0, b""

    def send(self, method, params=None):
        self.id += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}}) + "\n").encode())
        self.p.stdin.flush()
        return self.id

    def read(self, timeout=15):
        end = time.time() + timeout
        fd = self.p.stdout.fileno()
        while time.time() < end:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                return json.loads(line)
            if select.select([fd], [], [], 0.2)[0]:
                self.buf += os.read(fd, 65536)
        return None

    def rpc(self, method, params=None):
        i = self.send(method, params)
        while True:
            m = self.read()
            assert m is not None, f"no reply to {method}"
            if m.get("id") == i:
                return m["result"]

    def wait_note(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            m = self.read(1)
            if m and m.get("method") == "notifications/tools/list_changed":
                return True
        return False
