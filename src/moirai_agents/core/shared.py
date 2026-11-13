"""Changes to capabilities go to the group's shared store, agent-capabilities.

moirai-agents' capabilities directory (``.agents/capabilities``) is linked into the store at install time
(``python -m agent_capabilities link``): every entry is a link to ``capabilities/<topic>/<name>`` there, shared with
dftracer-agents and rabbit-agent. Each operation that changes a capability or its lessons is wrapped in
``shared_change``: it runs under the store's lock, and what it wrote is moved into the store, linked back, made
usable by the group (chgperm.sh) and committed, rebased and pushed (agent_capabilities.changing).

It is active only when the engine writes to this package's own capabilities directory and that directory is linked
into the store; tests (MOIRAI_AGENTS_*_CAPABILITIES) and a checkout that is not linked work as before.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

CAP_DIR = Path(__file__).resolve().parents[1] / ".agents" / "capabilities"
PROJECT = "moirai-agents"

try:
    from agent_capabilities import changing as _changing
    from agent_capabilities import store_root as _store_root
except ImportError:                     # agent-capabilities is not installed: the package's own store, as before
    _changing = None
    _store_root = None


def active() -> bool:
    """Whether changes go to the shared store now."""
    if _changing is None or not CAP_DIR.is_dir():
        return False
    if any(os.environ.get(f"MOIRAI_AGENTS_{k}_CAPABILITIES") for k in ("PROJECT", "USER", "BUNDLED")):
        return False
    root = str(_store_root().resolve())
    return any(p.is_symlink() and str(p.resolve()).startswith(root + os.sep) for p in CAP_DIR.iterdir())


def shared_change(what: str, describe=None):
    """Decorate an operation that changes capabilities or lessons. ``describe(result, args, kwargs)`` names what it
    did, for the commit message (default: what + the first argument)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not active():
                return fn(*args, **kwargs)
            with _changing(CAP_DIR, PROJECT, f"{what} {args[0] if args else ''}".strip()) as res:
                out = fn(*args, **kwargs)
                if describe is not None:
                    try:
                        res["message"] = describe(out, args, kwargs)
                    except Exception:           # the commit message is best effort
                        pass
                return out
        return wrapper
    return deco
