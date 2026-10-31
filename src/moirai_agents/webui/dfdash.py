"""dfdashboard, run by this server: one child process on a private port, started on first use, behind this server's port.

The page only ever talks to ``/dash/``: :func:`moirai_agents.webui.api.proxy` forwards it, so there is one URL, one token and one
tunnel. The child inherits the server's instance tag, so a shutdown or a sweep takes it down with everything else.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]


def executable() -> Optional[str]:
    env = os.environ.get("MOIRAI_AGENTS_DFDASHBOARD")
    if env:
        return env
    return shutil.which("dfdashboard-serve", path=str(Path(sys.executable).parent)) or shutil.which("dfdashboard-serve")


def web_dist() -> Optional[str]:
    """The built browser app. A wheel carries it; an editable checkout has it in web/dist."""
    env = os.environ.get("MOIRAI_AGENTS_DFDASHBOARD_DIST")
    if env:
        return env
    sibling = REPO.parent / "dfdashboard" / "web" / "dist"
    return str(sibling) if (sibling / "index.html").is_file() else None


class DfDash:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.port = 0
        self.proc: Optional[subprocess.Popen] = None
        self.error = ""
        self.lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, watch_root: str) -> Optional[int]:
        """The child's port, starting it if needed; None with ``error`` set when it cannot run."""
        with self.lock:
            if self.alive():
                return self.port
            exe = executable()
            if not exe:
                self.error = "dfdashboard is not installed: run scripts/setup_env.sh"
                return None
            os.makedirs(watch_root, exist_ok=True)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
            cmd = [exe, "--watch", watch_root, "--watch-interval", "1", "--host", "127.0.0.1", "--port", str(self.port), "--index-dir", str(self.state / "dfdashboard-index")]
            dist = web_dist()
            if dist:
                cmd += ["--web-dist", dist]
            log = open(self.state / "dfdashboard.log", "ab")
            self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            end = time.time() + 60
            while time.time() < end and self.proc.poll() is None:
                try:
                    socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                    self.error = ""
                    return self.port
                except OSError:
                    time.sleep(0.2)
            self.error = f"dfdashboard did not start: see {self.state / 'dfdashboard.log'}"
            return None

    def wait_run(self, watch_root: str, run: str, timeout: float = 10.0) -> bool:
        """Wait until dfdashboard lists a run: the page drops a run it does not know yet."""
        port = self.ensure(watch_root)
        end = time.time() + timeout
        while port and time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=3) as r:
                    if any(x.get("id") == run for x in json.load(r).get("runs", [])):
                        return True
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
