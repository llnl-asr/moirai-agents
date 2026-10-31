"""OpenAI for the agent: Claude Code speaks only the Anthropic Messages API, so an account whose model access is
OpenAI runs Claude Code against a private LiteLLM proxy that translates to OpenAI with that account's key.

One proxy per (key, base URL), started on first use and reused: bound to 127.0.0.1 on a free port and locked with a
random master key, so nobody else on a shared login node can use it (or the key behind it). The key reaches only
the proxy's environment; its config file (mode 600) names it by environment variable. LiteLLM lives in its own
venv (<repo>/.venv-bridge, or MOIRAI_AGENTS_LITELLM) because its proxy extras pin packages the agent's venv needs newer.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

OPENAI_URL = "https://api.openai.com/v1"
_SKIP = ("embed", "tts", "whisper", "dall-e", "image", "audio", "realtime", "moderation", "transcribe", "search",
         "davinci", "babbage", "instruct")


def litellm_bin(repo: Path) -> Optional[str]:
    for c in (os.environ.get("MOIRAI_AGENTS_LITELLM"), str(repo / ".venv-bridge" / "bin" / "litellm"), shutil.which("litellm")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def chat_models(ids: list) -> list:
    """Models an agent can chat with, newest-looking first."""
    keep = [m for m in ids if (m.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt")) or "/" in m)
            and not any(s in m for s in _SKIP)]
    return sorted(set(keep), key=lambda m: (not m.startswith("gpt-5"), not m.startswith("gpt"), m), reverse=False)


def list_models(key: str, base_url: str = OPENAI_URL) -> dict:
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"ok": False, "error": f"{base_url} rejected the key (HTTP {e.code}): {body[:200]}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"cannot reach {base_url}: {e}"}
    return {"ok": True, "models": chat_models([m.get("id", "") for m in data.get("data") or []])}


# Loaded by the proxy (litellm_settings.callbacks) when the upstream cannot stream (e.g. a gateway that resets
# every streaming request): LiteLLM then calls it without streaming and streams the finished answer to Claude Code.
NOSTREAM_HOOK = '''"""moirai-agents bridge: the upstream cannot stream; fake-stream every OpenAI chat call."""
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
from litellm.llms.openai.openai import OpenAIConfig


def _fake(self, model, stream, custom_llm_provider=None):
    return stream is True


OpenAIGPTConfig.should_fake_stream = _fake
OpenAIConfig.should_fake_stream = _fake
handler = CustomLogger()
'''


def upstream_streams(key: str, base_url: str, model: str) -> bool:
    """Does the endpoint answer a streaming chat request? (Some gateways reset every one.)"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "stream": True, "max_completion_tokens": 16,
                         "messages": [{"role": "user", "content": "Reply with OK."}]}).encode(),
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json", "accept": "text/event-stream"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return b"data:" in r.read(65536)
    except (urllib.error.URLError, OSError):
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridges:
    def __init__(self, state: Path, repo: Path) -> None:
        self.root = Path(state) / "bridge"
        self.repo = repo
        self.procs: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def _alive(self, b: dict) -> bool:
        if b["proc"].poll() is not None:
            return False
        try:
            urllib.request.urlopen(f"{b['url']}/health/liveliness", timeout=3).read()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def get(self, key: str, base_url: str = OPENAI_URL, stream: bool = True, wait: Optional[float] = None) -> dict:
        """-> {url, token} of a running proxy for this key (started if needed). stream=False: the upstream cannot
        stream, the proxy fakes it. Callers arriving while it starts wait for that start. Raises RuntimeError."""
        wait = wait or float(os.environ.get("MOIRAI_AGENTS_BRIDGE_START_S", "180"))
        fp = hashlib.sha256(f"{key}\0{base_url}\0{stream}".encode()).hexdigest()[:16]
        with self.lock:
            b = self.procs.get(fp)
            starting = b is not None and not b["ev"].is_set()
            if b and not starting and self._alive(b):
                b["used"] = time.time()
                return {"url": b["url"], "token": b["token"]}
            if b and not starting:
                self._kill(fp)
        if starting:                                     # someone else is starting it: share that start
            b["ev"].wait(wait)
            if b.get("error") or not b["ev"].is_set():
                raise RuntimeError(b.get("error") or "the OpenAI bridge is still starting; try again")
            b["used"] = time.time()
            return {"url": b["url"], "token": b["token"]}
        with self.lock:
            exe = litellm_bin(self.repo)
            if not exe:
                raise RuntimeError("the OpenAI bridge (LiteLLM) is not installed on this server: the server owner runs "
                                   "scripts/setup_openai_bridge.sh once (no restart needed)")
            d = self.root / fp
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            params = {"model": "openai/*", "api_key": "os.environ/OPENAI_API_KEY"}
            if base_url.rstrip("/") != OPENAI_URL:
                params["api_base"] = "os.environ/OPENAI_API_BASE"
            cfg = {"model_list": [{"model_name": "*", "litellm_params": params}],
                   "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
                   "litellm_settings": {"drop_params": True, "telemetry": False}}
            if not stream:
                (d / "bridge_nostream.py").write_text(NOSTREAM_HOOK)
                cfg["litellm_settings"]["callbacks"] = ["bridge_nostream.handler"]
            cf = d / "config.yaml"
            fd = os.open(cf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(cfg, fh)                       # JSON is YAML
            token, port = "sk-" + secrets.token_urlsafe(24), _free_port()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "LIVAI_", "OPENAI_", "MOIRAI_AGENTS_OWNER"))}
            env.update(OPENAI_API_KEY=key, OPENAI_API_BASE=base_url, LITELLM_MASTER_KEY=token,
                       LITELLM_TELEMETRY="False", LITELLM_LOG="ERROR")
            if base_url.rstrip("/") != OPENAI_URL:
                # OpenAI itself gets the Responses API; OpenAI-compatible gateways speak chat/completions
                env["LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES"] = "True"
            log = open(d / "proxy.log", "ab")
            proc = subprocess.Popen([exe, "--config", str(cf), "--host", "127.0.0.1", "--port", str(port)],
                                    cwd=str(d), env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            b = self.procs[fp] = {"proc": proc, "url": f"http://127.0.0.1:{port}", "token": token,
                                  "used": time.time(), "log": str(d / "proxy.log"), "ev": threading.Event()}
        end = time.time() + wait
        while time.time() < end:
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"{b['url']}/health/liveliness", timeout=3).read()
                b["ev"].set()
                return {"url": b["url"], "token": token}
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        tail = Path(b["log"]).read_text(errors="replace")[-400:] if Path(b["log"]).exists() else ""
        why = "exited" if proc.poll() is not None else f"did not answer within {wait:.0f} s (MOIRAI_AGENTS_BRIDGE_START_S)"
        b["error"] = f"the OpenAI bridge {why}; its log {b['log']} ends with: {tail.strip() or 'no output'}"
        b["ev"].set()
        with self.lock:
            self._kill(fp)
        raise RuntimeError(b["error"])

    def running(self, key: str, base_url: str = OPENAI_URL, stream: bool = True) -> bool:
        fp = hashlib.sha256(f"{key}\0{base_url}\0{stream}".encode()).hexdigest()[:16]
        b = self.procs.get(fp)
        return bool(b and b["ev"].is_set() and not b.get("error") and b["proc"].poll() is None)

    def check(self, key: str, base_url: str, model: str, stream: bool = True) -> dict:
        """End to end, the way the agent uses it: one streamed Anthropic-format message through the bridge."""
        try:
            b = self.get(key, base_url, stream)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        req = urllib.request.Request(
            b["url"] + "/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 64, "stream": True,
                             "messages": [{"role": "user", "content": "Reply with OK."}]}).encode(),
            headers={"x-api-key": b["token"], "Authorization": f"Bearer {b['token']}",
                     "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            sse = urllib.request.urlopen(req, timeout=180).read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"{model} through the bridge failed (HTTP {e.code}): "
                    f"{e.read().decode(errors='replace')[:300]}"}
        except (urllib.error.URLError, OSError) as e:
            return {"ok": False, "error": f"the bridge did not answer: {e}"}
        if "message_stop" not in sse or "event: error" in sse or '"type": "error"' in sse or '"type":"error"' in sse:
            return {"ok": False, "error": f"unexpected reply through the bridge: {sse[-300:]}"}
        return {"ok": True}

    def _kill(self, fp: str) -> None:
        b = self.procs.pop(fp, None)
        if b and b["proc"].poll() is None:
            b["proc"].terminate()
            try:
                b["proc"].wait(5)
            except subprocess.TimeoutExpired:
                b["proc"].kill()

    def reap(self, idle: float = 4 * 3600) -> None:
        """Stop proxies nobody used for `idle` seconds (a running turn keeps using its own)."""
        with self.lock:
            for fp, b in list(self.procs.items()):
                if time.time() - b["used"] > idle or b["proc"].poll() is not None:
                    self._kill(fp)

    def stop_all(self) -> None:
        with self.lock:
            for fp in list(self.procs):
                self._kill(fp)
