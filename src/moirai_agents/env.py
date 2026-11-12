"""The repo's .env (read once at startup; existing environment variables win), the system this runs on, its venv, and the group umask.

One checkout can serve several systems (a shared file system mounted on mammoth, tuolumne, ...). Each system has its own venv and may
have its own settings: the system is the short hostname without its node number (mammoth34 -> mammoth, tuolumne1009 -> tuolumne),
or MOIRAI_AGENTS_SYSTEM. Its venv is <repo>/.venv-<system>, or MOIRAI_AGENTS_VENV. Settings come from .env.<system> first, then .env.
scripts/venv.sh is the same logic for the shell scripts and for the workspace's MCP servers and hooks.
"""
from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("MOIRAI_AGENTS_ENV_FILE", REPO / ".env"))
VENV_SH = REPO / "scripts" / "venv.sh"


def system_name() -> str:
    """The system, not the node: the short hostname with its trailing node number removed."""
    s = os.environ.get("MOIRAI_AGENTS_SYSTEM", "").strip()
    if not s:
        host = socket.gethostname().split(".")[0]
        s = re.sub(r"[-_]?\d+$", "", host) or host
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s) or "default"


def system_env_file() -> Path:
    """This system's settings, read before .env: .env.<system> beside .env."""
    return ENV_FILE.with_name(f"{ENV_FILE.name}.{system_name()}")


def _file_value(f: Path, name: str):
    try:
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def env_file_value(name: str):
    """NAME from .env.<system>, else .env, read fresh (no restart after an edit)."""
    return _file_value(system_env_file(), name) or _file_value(ENV_FILE, name)


def venv_dir() -> Path:
    """This system's venv: MOIRAI_AGENTS_VENV (environment, .env.<system> or .env), else <repo>/.venv-<system>."""
    v = os.environ.get("MOIRAI_AGENTS_VENV") or env_file_value("MOIRAI_AGENTS_VENV")
    return Path(v).expanduser() if v else REPO / f".venv-{system_name()}"


def venv_python() -> str:
    """This system's venv python, or the running interpreter when the venv is not built yet."""
    py = venv_dir() / "bin" / "python"
    return str(py if py.exists() else sys.executable)


def state_dir(*parts: str) -> Path:
    """Where moirai-agents keeps its own state (user capabilities, learning, web UI state, caches): inside the project, never in the
    home directory. MOIRAI_AGENTS_HOME moves it; the default is <repo>/workspace/.moirai-agents (gitignored, beside the pipelines)."""
    return Path(os.environ.get("MOIRAI_AGENTS_HOME") or REPO / "workspace" / ".moirai-agents").expanduser().joinpath(*parts)


def load_env() -> None:
    for f in (system_env_file(), ENV_FILE):         # setdefault: the environment wins, then the system's file, then .env
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.replace("export ", "").strip()
            os.environ.setdefault(k, v.strip().strip("'\""))
    os.environ.setdefault("MOIRAI_AGENTS_SYSTEM", system_name())
    os.environ.setdefault("DFTRACER_JIT_CACHE", str(state_dir("cache", "dftracer-utils", "jit")))     # dftracer-utils: ~/.cache otherwise
    os.environ.setdefault("DFTRACER_UTILS_SPILL_DIR", str(state_dir("cache", "dftracer-utils", "spill")))


def group_umask() -> None:
    os.umask(0o007)
