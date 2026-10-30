"""Model access (ModelConfig): you pick the mechanism explicitly - livai, anthropic or openai - and each one is verified with a real call before
it is saved. Several can be configured; one is active, and switching is one call. The config holds your keys: a file of mode 600.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

PROVIDERS = ("livai", "anthropic", "openai")
LABEL = {"livai": "LivAI", "anthropic": "Anthropic", "openai": "OpenAI"}
NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
ITER = 200_000


def _write_private(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


class ModelConfig:
    """{"active": provider, "providers": {provider: {key, base_url?, cred?, model, models, verified}}}"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        d.setdefault("providers", {})
        if d.get("active") not in d["providers"]:
            d["active"] = next(iter(d["providers"]), None)
        return d

    def save(self, d: dict) -> None:
        _write_private(self.path, d)

    def put(self, provider: str, entry: dict, activate: bool = True) -> dict:
        d = self.load()
        d["providers"][provider] = entry
        if activate or not d.get("active"):
            d["active"] = provider
        self.save(d)
        return d

    def activate(self, provider: str) -> dict:
        d = self.load()
        if provider not in d["providers"]:
            raise KeyError(provider)
        d["active"] = provider
        self.save(d)
        return d

    def remove(self, provider: str) -> dict:
        d = self.load()
        d["providers"].pop(provider, None)
        if d.get("active") == provider:
            d["active"] = next(iter(d["providers"]), None)
        self.save(d)
        return d

    def active(self) -> tuple:
        d = self.load()
        p = d.get("active")
        return (p, d["providers"][p]) if p else (None, None)

    @staticmethod
    def public(d: dict) -> dict:
        """What the page may see: never the keys themselves."""
        out = {}
        for p, e in d.get("providers", {}).items():
            k = e.get("key") or ""
            out[p] = {"label": LABEL.get(p, p), "cred": e.get("cred"), "base_url": e.get("base_url"),
                      "model": e.get("model"), "models": e.get("models") or [], "verified": e.get("verified"),
                      "key_hint": ("…" + k[-4:]) if len(k) > 8 else ("this account's Claude login" if k == "login" else "")}
        return {"active": d.get("active"), "providers": out}
