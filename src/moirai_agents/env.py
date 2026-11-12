"""The repo's .env (read once at startup; existing environment variables win) and the group umask."""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("MOIRAI_AGENTS_ENV_FILE", REPO / ".env"))


def state_dir(*parts: str) -> Path:
    """Where moirai-agents keeps its own state (user capabilities, learning, web UI state, caches): inside the project, never in the
    home directory. MOIRAI_AGENTS_HOME moves it; the default is <repo>/workspace/.moirai-agents (gitignored, beside the pipelines)."""
    return Path(os.environ.get("MOIRAI_AGENTS_HOME") or REPO / "workspace" / ".moirai-agents").expanduser().joinpath(*parts)


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        os.environ.setdefault(k, v.strip().strip("'\""))
    os.environ.setdefault("DFTRACER_JIT_CACHE", str(state_dir("cache", "dftracer-utils", "jit")))     # dftracer-utils: ~/.cache otherwise
    os.environ.setdefault("DFTRACER_UTILS_SPILL_DIR", str(state_dir("cache", "dftracer-utils", "spill")))


def group_umask() -> None:
    os.umask(0o007)
