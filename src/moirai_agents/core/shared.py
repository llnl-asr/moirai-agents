"""Where moirai-agents' capability changes go: its own capabilities stay here; a change to a dftracer-agents or
phronix capability is written in this project's contribution worktree of that project and committed on
contrib/moirai-agents (phronix.engine.config.shared_change, phronix.engine.upstream)."""
from . import _engine  # noqa: F401
from phronix.engine.config import shared_change

CAP_DIR = _engine.PKG / ".agents" / "capabilities"
PROJECT = "moirai-agents"

__all__ = ["shared_change", "CAP_DIR", "PROJECT"]
