
import argparse
import base64
import errno
import hashlib
import json
import re
import urllib.error
import urllib.request
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import sys
import threading
import time
import traceback
import uuid
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from .accounts import LABEL, PROVIDERS, ModelConfig
from .bridge import OPENAI_URL, Bridges, list_models, upstream_streams

PKG = Path(__file__).resolve().parents[1]           # src/moirai_agents
REPO = PKG.parents[1]                                # moirai-agents checkout
STATIC = Path(__file__).resolve().parent / "static"
WS = Path(os.environ.get("MOIRAI_AGENTS_WORKSPACE", REPO / "workspace"))   # where claude sessions run
STATE = Path(os.environ.get("MOIRAI_AGENTS_WEBUI_STATE", Path.home() / ".moirai-agents" / "webui"))

from ..env import ENV_FILE, load_env  # noqa: E402

load_env()
from ..env import group_umask  # noqa: E402
from . import procs  # noqa: E402

group_umask()  # group-usable files in the workspace
MODELS = [m.strip() for m in os.environ.get(
    "MOIRAI_AGENTS_MODELS", "claude-sonnet-4.5,gpt-5.5,gpt-5.6-sol,gpt-5.4"
).split(",") if m.strip()]

# Workspace sessions get every tool; safety comes from the workspace settings (deny rules for
# sudo / rm -rf / edits of the package), not from the mode.
ALL_TOOLS = ["Bash", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS",
             "WebFetch", "WebSearch", "Task", "TodoWrite", "Skill", "mcp__moirai", "mcp__dftracer"]
APPS_DIR = Path(os.environ.get("MOIRAI_AGENTS_APPS_DIR", str(REPO / "workspace" / "apps")))
LIVAI_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://livai-api.llnl.gov")
PROVENANCE = Path(os.environ.get("MOIRAI_AGENTS_PROVENANCE_DIR", REPO / "logs" / "provenance"))
PROV_MAX_LINE = 2_000_000
# Uploads: kept privately with their chat in the user's folder (<ws>/.moirai-agents/uploads/<chat id>, mode 700) and
# deleted with the chat (its retention, or when the user deletes it). MOIRAI_AGENTS_UPLOADS_KEEP=run restores the old
# behaviour (deleted when the run ends). Their CONTENT never goes into chats or provenance either way.
UPLOAD_MAX_FILE = int(os.environ.get("MOIRAI_AGENTS_UPLOAD_MAX_MB", "25")) * 1024 * 1024
UPLOAD_MAX_TOTAL = 3 * UPLOAD_MAX_FILE
UPLOADS_KEEP = os.environ.get("MOIRAI_AGENTS_UPLOADS_KEEP", "chat")         # chat | run
UPLOAD_REDACTED = ("[content of an uploaded file — not stored in the chat; the file is in this chat's private uploads "
                   "folder]" if UPLOADS_KEEP == "chat" else
                   "[content of an uploaded file — not stored; the file was deleted after the run]")


def uploads_dir(workspace, sid: str) -> Path:
    """The private uploads folder of one chat."""
    return Path(workspace) / ".moirai-agents" / "uploads" / re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "nochat")
REGISTRY = None   # procs.Registry of the running server (state file used by webui_stop.sh)
LIVE_POLL, LIVE_CHUNK, LIVE_TAIL = 0.7, 256 * 1024, 64 * 1024   # live Bash output: poll s, max read, kept tail

# --------------------------------------------------------------- utilities
def _now() -> float:
    return time.time()


class Store:
    """Sessions + transcripts on disk (one JSON per session)."""

    def __init__(self, root: Path) -> None:
        self.root = root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def get(self, sid: str) -> Optional[dict]:
        p = self.path(sid)
        return json.loads(p.read_text()) if p.exists() else None

    def put(self, s: dict) -> None:
        with self.lock:
            tmp = self.path(s["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(s))
            os.replace(tmp, self.path(s["id"]))

    def list(self) -> list:
        out = []
        for p in sorted(self.root.glob("*.json"), key=lambda p: -p.stat().st_mtime):
            try:
                s = json.loads(p.read_text())
            except ValueError:
                continue
            row = {k: s.get(k) for k in ("id", "title", "mode", "app_dir", "model", "created", "updated", "archived")}
            out.append(row)
        return out

    def archive(self, sid: str, on: bool = True) -> bool:
        with self.lock:
            s = self.get(sid)
            if s is None:
                return False
            if on:
                s["archived"] = _now()
            else:
                s.pop("archived", None)
                s["updated"] = _now()          # restored: the active-chat clock starts again
            tmp = self.path(sid).with_suffix(".tmp")
            tmp.write_text(json.dumps(s))
            os.replace(tmp, self.path(sid))
        return True

    def delete(self, sid: str) -> None:
        self.path(sid).unlink(missing_ok=True)
        shutil.rmtree(self.path(sid).parent.parent / "uploads" / sid, ignore_errors=True)   # the chat's uploads go with it


# Model providers, told apart by the key a user signs in with:
#   livai     - LivAI key (LLNL gateway, ANTHROPIC_BASE_URL=LivAI)
#   claude    - Claude Pro/Max subscription token from `claude setup-token` (sk-ant-oat...)
#   anthropic - Anthropic API key (sk-ant-api...)
ANTHROPIC_MODELS = [m.strip() for m in os.environ.get("MOIRAI_AGENTS_ANTHROPIC_MODELS", "sonnet,opus,haiku").split(",")
                    if m.strip()]   # Claude Code aliases -> the current models of each family
SECRET_ENV = ("LIVAI_API_KEY", "OPENAI_API_KEY", "MOIRAI_AGENTS_LIVAI_KEY", "MOIRAI_AGENTS_OWNER_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # never inherited by a user's agent


OWNER_LOGIN = "login"   # MOIRAI_AGENTS_OWNER_KEY=login: the owner link uses this account's own `claude` login


def provider_of(key: Optional[str]) -> str:
    k = (key or "").strip()
    if k == OWNER_LOGIN:
        return "login"
    return "claude" if k.startswith("sk-ant-oat") else "anthropic" if k.startswith("sk-ant-api") else "livai"


def claude_home() -> Path:
    """The server account's own Claude Code config dir (its subscription login lives here)."""
    return Path(os.environ.get("MOIRAI_AGENTS_OWNER_CLAUDE_HOME") or Path.home() / ".claude")


def provider_models(provider: str) -> list:
    return ANTHROPIC_MODELS if provider in ("claude", "anthropic", "login") else MODELS


def _env_file_value(name: str) -> Optional[str]:
    f = os.environ.get("MOIRAI_AGENTS_LIVAI_ENV", str(ENV_FILE))
    try:
        for line in open(f):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def server_key() -> Optional[str]:
    """The server owner's key from .env (used only for the owner login): MOIRAI_AGENTS_OWNER_KEY (any provider,
    e.g. a `claude setup-token` token), else the LivAI key."""
    key = _env_file_value("MOIRAI_AGENTS_OWNER_KEY") or os.environ.get("MOIRAI_AGENTS_OWNER_KEY")   # .env read fresh
    key = key or os.environ.get("MOIRAI_AGENTS_LIVAI_KEY") or os.environ.get("LIVAI_API_KEY")
    key_file = os.environ.get("MOIRAI_AGENTS_LIVAI_ENV", str(ENV_FILE))
    if not key and os.path.isfile(key_file):
        for line in open(key_file):
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith(("LIVAI_API_KEY=", "OPENAI_API_KEY=", "ANTHROPIC_API_KEY=")):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return key


def validate_key(key: str, model: str) -> dict:
    """Is the key valid, and which models may it use? (LivAI, Claude subscription or Anthropic API key)"""
    prov = provider_of(key)
    if prov == "login" and not (claude_home() / ".credentials.json").exists():
        return {"ok": False, "provider": prov, "error": f"no Claude Code login in {claude_home()} (run `claude` and /login)"}
    if prov == "anthropic":
        req = urllib.request.Request("https://api.anthropic.com/v1/models",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return {"ok": True, "models": ANTHROPIC_MODELS, "provider": prov}
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"Anthropic rejected the API key (HTTP {e.code})"}
        except (urllib.error.URLError, OSError) as e:
            return {"ok": False, "error": f"cannot reach api.anthropic.com: {e}"}
    if prov in ("claude", "login"):
        # no lightweight endpoint for subscription tokens / a login: one tiny Claude Code call with it
        claude = claude_bin()
        try:
            r = subprocess.run([claude, "-p", "Reply with OK.", "--model", "haiku", "--output-format", "json",
                                "--strict-mcp-config", "--tools", ""],
                               env=livai_env("haiku", key), capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
            out = json.loads(r.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return {"ok": False, "error": f"could not check the token with Claude Code: {e}"}
        if out.get("is_error") or r.returncode != 0:
            return {"ok": False, "error": f"Claude rejected the {'login' if prov == 'login' else 'subscription token'}: "
                    + str(out.get("result") or r.stderr[-300:] or "unknown error")[:300]}
        return {"ok": True, "models": ANTHROPIC_MODELS, "provider": prov}
    req = urllib.request.Request(
        LIVAI_URL.rstrip("/") + "/v1/messages",
        data=json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return {"ok": True, "models": None}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        m = re.search(r"can only access models=\[([^\]]*)\]", body)
        if m:
            allowed = [x.strip().strip("'\"") for x in m.group(1).split(",")]
            allowed = [x for x in allowed if x and "embedding" not in x and "ada" not in x]
            return {"ok": True, "models": allowed}
        msg = re.search(r'"message":\s*"([^"]+)', body)
        if e.code in (404, 502, 503, 504) and not msg:      # gateway down: the key was never checked
            return {"ok": False, "outage": True, "error": (
                f"LivAI is not answering right now (its gateway returns HTTP {e.code} to every request), so your "
                "key could not be checked - it is probably fine. Try again later, or sign in with a Claude "
                "subscription token (run `claude setup-token` on your computer and paste the sk-ant-oat01-... token).")}
        return {"ok": False, "error": (msg.group(1) if msg else body[:300]) or f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "error": f"cannot reach LivAI: {e}"}


def livai_env(model: str, key: Optional[str] = None) -> Dict[str, str]:
    """Environment for a user's Claude Code process: routed to LivAI (see dftracer-agents
    scripts/pecan-livai.sh), or straight to Anthropic for a subscription token / API key."""
    env = dict(os.environ)
    for k in SECRET_ENV:                         # never leak the server's (or anyone else's) credentials
        env.pop(k, None)
    if key is None:
        key = server_key()
    prov = provider_of(key)
    if prov != "livai":
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
            env.pop(k, None)
        if prov == "login":
            env["CLAUDE_CONFIG_DIR"] = str(claude_home())   # its own login; tokens refresh in place
        else:
            env["CLAUDE_CODE_OAUTH_TOKEN" if prov == "claude" else "ANTHROPIC_API_KEY"] = key
        env["ANTHROPIC_MODEL"] = model
        env.setdefault("DISABLE_TELEMETRY", "1")
        _venv_paths(env)
        return env
    env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL", "https://livai-api.llnl.gov")
    if key:
        env["ANTHROPIC_AUTH_TOKEN"] = key
        env["ANTHROPIC_API_KEY"] = key
    env["ANTHROPIC_MODEL"] = model
    env["ANTHROPIC_SMALL_FAST_MODEL"] = os.environ.get("MOIRAI_AGENTS_SMALL_MODEL", model)
    env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1000000")
    env.setdefault("MAX_MCP_OUTPUT_TOKENS", "200000")
    env.setdefault("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", "200000")
    env.setdefault("DISABLE_TELEMETRY", "1")
    _venv_paths(env)
    return env


def wlog(event: str, **kv) -> None:
    """One line in the web UI's log (logs/webui/webui-<host>-<time>.log, stdout of the server) per thing worth
    debugging later: turn start/end, MCP servers that failed to start (with the reason and its log), the OpenAI
    bridge, Claude Code problems. Never a key."""
    parts = " ".join(f"{k}={json.dumps(v) if isinstance(v, (str, list, dict)) and (not isinstance(v, str) or ' ' in v or not v) else v}"
                     for k, v in kv.items() if v is not None)
    print(f"{time.strftime('%F %T')} [{event}] {parts}", flush=True)


def mcp_log_dir(cwd: str, server: str) -> Path:
    """Where Claude Code writes the log of one MCP server for sessions started in `cwd` (account running the server)."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "claude-cli-nodejs" / slug / f"mcp-logs-{server}"


def mcp_failure(cwd: str, server: str) -> dict:
    """Why an MCP server failed to start: the last error of its newest Claude Code log. -> {reason, log}"""
    d = mcp_log_dir(cwd, server)
    try:
        f = max(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)
    except (ValueError, OSError):
        return {"reason": f"no Claude Code log for it in {d}", "log": None}
    crash = error = debug = ""
    try:
        for line in f.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for msg in (rec.get("error") or "", rec.get("debug") or ""):
                if "Traceback (most recent call last)" in msg:
                    # the server crashed: its exception line says why (e.g. ModuleNotFoundError: No module named x)
                    tail = [ln.strip() for ln in msg.splitlines() if ln.strip()]
                    exc = next((ln for ln in reversed(tail) if re.match(r"^[A-Za-z_.]+(Error|Exception|Exit)\b", ln)), tail[-1])
                    where = next((ln for ln in reversed(tail) if ln.startswith("File ")), "")
                    crash = f"the server crashed at start: {exc}" + (f" ({where})" if where else "")
            msg = rec.get("error") or ""
            if msg and "Component already exists" not in msg and "INFO " not in msg[:40] and "Traceback" not in msg:
                error = msg
            dbg = rec.get("debug") or ""
            if (re.search(r"timed out|timeout|failed|ENOENT|EACCES|not found|exited", dbg, re.I)
                    and not re.search(r"negotiation|probe|respawn", dbg, re.I)):   # Claude Code's own retries
                debug = dbg
    except OSError:
        pass
    reason = crash or error or debug
    return {"reason": (reason or "no error recorded (see the log)").strip()[-600:], "log": str(f)}


_CLAUDE: Dict[str, object] = {}
CLAUDE_SETUP = ("scripts/setup_claude.sh   (installs Claude Code if needed, records MOIRAI_AGENTS_CLAUDE_BIN in .env, "
                "checks it; no restart needed)")


def find_claude(refresh: bool = False) -> dict:
    """Where Claude Code is and whether it runs. -> {ok, path, version, problem, error, searched, fix}.
    Looks at MOIRAI_AGENTS_CLAUDE_BIN (.env, read fresh), PATH, then the usual install locations, so a claude that is
    installed but missing from the server's PATH is used anyway. Cached for a minute."""
    configured = _env_file_value("MOIRAI_AGENTS_CLAUDE_BIN") or os.environ.get("MOIRAI_AGENTS_CLAUDE_BIN")
    sig = (configured, os.environ.get("PATH"), os.environ.get("HOME"))      # a new setting is seen at once
    if not refresh and _CLAUDE.get("sig") == sig and _CLAUDE.get("t", 0) > time.time() - 60:
        return _CLAUDE["v"]
    home = Path.home()
    cands = [configured, shutil.which("claude"), str(home / ".local" / "bin" / "claude"),
             str(home / ".claude" / "local" / "claude"), str(home / ".npm-global" / "bin" / "claude"),
             "/usr/local/bin/claude"]
    searched = [c for c in dict.fromkeys(cands) if c]
    out = {"ok": False, "path": None, "version": None, "searched": searched}
    if configured and not os.path.exists(configured):
        out.update(problem="configured_missing", error=f"MOIRAI_AGENTS_CLAUDE_BIN={configured} does not exist")
    else:
        for c in searched:
            if not os.path.isfile(c):
                continue
            out["path"] = c
            if not os.access(c, os.X_OK):
                out.update(problem="not_executable", error=f"{c} is not executable (chmod +x it, or reinstall)")
                continue
            try:
                r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                out.update(problem="timeout", error=f"{c} --version did not answer in 45 s (a stuck update, or a "
                                                    "slow file system); it will be retried")
                continue
            except OSError as e:
                out.update(problem="cannot_run", error=f"{c} cannot run: {e}")
                continue
            if r.returncode != 0 or not r.stdout.strip():
                out.update(problem="crashed", error=f"{c} --version failed (exit {r.returncode}): "
                                                    f"{(r.stderr or r.stdout).strip()[-300:] or 'no output'}")
                continue
            out.update(ok=True, path=c, version=r.stdout.strip(), problem=None, error=None)
            break
        else:
            if not out.get("problem"):
                out.update(problem="not_found", error="Claude Code (the `claude` program) is not installed for the "
                                                      f"server account {os.environ.get('USER', '')}: looked in "
                                                      + ", ".join(searched))
    out["fix"] = None if out["ok"] else [
        f"On the server, as the account that runs the web UI: {CLAUDE_SETUP}",
        "Or by hand: curl -fsSL https://claude.ai/install.sh | bash   (or: npm install -g @anthropic-ai/claude-code)",
        "then put MOIRAI_AGENTS_CLAUDE_BIN=/full/path/to/claude in moirai-agents/.env  (`command -v claude` prints it)",
        "Check: <path> --version prints a version. Then reload this page."]
    _CLAUDE.update(t=time.time(), v=out, sig=sig)
    return out


def set_env_value(name: str, value: str) -> Path:
    """Write NAME=value into the server's .env (replacing an existing line; mode 600). Read fresh, so no restart."""
    f = Path(os.environ.get("MOIRAI_AGENTS_LIVAI_ENV", str(ENV_FILE)))
    lines = f.read_text().splitlines() if f.exists() else []
    rx = re.compile(rf"^\s*(export\s+)?{re.escape(name)}=")
    new = [ln for ln in lines if not rx.match(ln)] + [f"{name}={value}"]
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(new) + "\n")
    os.chmod(f, 0o600)                       # it holds keys: private even if it existed with looser bits
    os.environ[name] = value
    return f


def use_claude_path(path: str) -> dict:
    """The owner points the server at a claude binary from the UI: checked by running it, then recorded."""
    p = os.path.abspath(os.path.expanduser(os.path.expandvars((path or "").strip())))
    if os.path.isdir(p):
        p = os.path.join(p, "claude")
    if not os.path.isfile(p):
        return {"ok": False, "error": f"{p} does not exist"}
    if not os.access(p, os.X_OK):
        return {"ok": False, "error": f"{p} is not executable"}
    try:
        r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"{p} --version did not work: {e}"}
    if r.returncode != 0 or "claude" not in (r.stdout + r.stderr).lower():
        return {"ok": False, "error": f"{p} does not look like Claude Code: {(r.stdout or r.stderr).strip()[:200]}"}
    f = set_env_value("MOIRAI_AGENTS_CLAUDE_BIN", p)
    return dict(find_claude(refresh=True), saved_to=str(f))


def install_claude() -> dict:
    """Run scripts/setup_claude.sh --yes as the server account (owner only): install if needed, record, check."""
    script = REPO / "scripts" / "setup_claude.sh"
    try:
        r = subprocess.run(["bash", str(script), "--yes"], capture_output=True, text=True, timeout=900,
                           stdin=subprocess.DEVNULL, cwd=str(REPO))
        out = (r.stdout + r.stderr)[-3000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        out = f"{script} failed: {e}"
    return dict(find_claude(refresh=True), log=out)


def claude_bin() -> str:
    c = find_claude()
    return c["path"] if c["ok"] else (os.environ.get("MOIRAI_AGENTS_CLAUDE_BIN") or shutil.which("claude") or "claude")


BRIDGES: Optional[Bridges] = None     # OpenAI bridges of this server (set by App)


def bridge_env(model: str, url: str, token: str) -> Dict[str, str]:
    """Claude Code against an account's private LiteLLM bridge to OpenAI: every model role is the chosen model."""
    env = dict(os.environ)
    for k in SECRET_ENV + ("ANTHROPIC_BASE_URL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        env.pop(k, None)
    env.update(ANTHROPIC_BASE_URL=url, ANTHROPIC_AUTH_TOKEN=token, ANTHROPIC_MODEL=model,
               ANTHROPIC_SMALL_FAST_MODEL=model, ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
               ANTHROPIC_DEFAULT_SONNET_MODEL=model, ANTHROPIC_DEFAULT_OPUS_MODEL=model,
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
               # OpenAI takes at most 128 tools per request and a turn has ~350 (built-ins + MCP servers):
               # MCP tools are looked up on demand (tool search) instead of all being sent every time
               ENABLE_TOOL_SEARCH="true")
    env.setdefault("MAX_MCP_OUTPUT_TOKENS", "200000")
    env.setdefault("DISABLE_TELEMETRY", "1")
    _venv_paths(env)
    return env


def user_bridge(user: dict) -> dict:
    """Start (or reuse) the OpenAI bridge of this account. Can take a while on first use: never in a request."""
    return BRIDGES.get(user["key"], user.get("base_url") or OPENAI_URL, user.get("stream", True) is not False)


def prewarm_bridge(user: dict) -> None:
    """An OpenAI account signs in or switches to OpenAI: start its bridge now, in the background."""
    if user.get("provider") == "openai" and BRIDGES is not None:
        threading.Thread(target=lambda: _quiet(user_bridge, user), daemon=True).start()


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # the turn that needs it reports the error
        print(f"bridge prewarm: {e}", flush=True)


def start_bridges(state: Path) -> list:
    """At server start: the bridge of your OpenAI access, started in parallel and
    waited for, so chats just use them. -> [(who, ok, detail)]"""
    seen, jobs = set(), []
    for f in [OWNER_DIR / "model.json"]:
        e = ModelConfig(f).load()["providers"].get("openai")
        if not e or not e.get("key"):
            continue
        k = (e["key"], e.get("base_url") or OPENAI_URL, e.get("stream", True) is not False)
        if k not in seen:
            seen.add(k)
            jobs.append(("owner" if f.parent == OWNER_DIR else f.parent.name, k))
    out = []

    def one(who, k):
        try:
            BRIDGES.get(*k)
            out.append((who, True, k[1]))
        except RuntimeError as e:
            out.append((who, False, str(e)[:300]))
    ts = [threading.Thread(target=one, args=j, daemon=True) for j in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def agent_env(user: dict, model: str, bridge: Optional[dict] = None) -> Dict[str, str]:
    """The agent's environment for the account's active model access (livai | anthropic | openai). For OpenAI,
    `bridge` is the running bridge; without it the bridge settings are left to fill in (Turn starts it)."""
    prov = user.get("provider")
    if not prov:
        raise RuntimeError("no model access configured: open Model settings and verify LivAI, Anthropic or OpenAI")
    if prov == "openai":
        b = bridge or {"url": "", "token": ""}
        return bridge_env(model, b["url"], b["token"])
    return livai_env(model, user.get("key"))       # livai key, or an Anthropic key / token / the owner's login


def verify_provider(provider: str, key: str, model: Optional[str], base_url: Optional[str], owner: bool) -> dict:
    """Check model access with a real call, the way the agent will use it. -> {ok, models, model, cred, error}"""
    key = (key or "").strip()
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider {provider!r}"}
    if not key:
        return {"ok": False, "error": f"{LABEL[provider]}: a key is required"}
    if provider == "livai":
        if key.startswith("sk-ant-") or key == OWNER_LOGIN:
            return {"ok": False, "error": "that is an Anthropic key or token: choose Anthropic"}
        v = validate_key(key, model or os.environ.get("MOIRAI_AGENTS_MODEL", MODELS[0]))
        models = v.get("models") or MODELS
    elif provider == "anthropic":
        if key == OWNER_LOGIN and not owner:
            return {"ok": False, "error": "not a valid key"}
        if key != OWNER_LOGIN and not key.startswith(("sk-ant-api", "sk-ant-oat")):
            return {"ok": False, "error": "Anthropic: paste an API key (sk-ant-api…) or a subscription token "
                                          "(sk-ant-oat… from `claude setup-token`)"}
        v = validate_key(key, model or ANTHROPIC_MODELS[0])
        models = ANTHROPIC_MODELS
    else:
        base = (base_url or OPENAI_URL).strip().rstrip("/")
        v = list_models(key, base)
        models = v.get("models") or []
        if v["ok"] and not models:
            v = {"ok": False, "error": f"{base} lists no chat models for this key"}
        streams = True
        if v["ok"]:
            pick = model if model in models else models[0]
            streams = base == OPENAI_URL or upstream_streams(key, base, pick)
            c = BRIDGES.check(key, base, pick, streams)
            v = dict(v, ok=c["ok"], error=c.get("error"))
    if not v.get("ok"):
        return {"ok": False, "error": f"{LABEL[provider]}: {v.get('error') or 'check failed'}"}
    return {"ok": True, "models": models, "model": model if model in models else models[0],
            "cred": provider_of(key) if provider == "anthropic" else provider,
            "base_url": (base_url or OPENAI_URL).strip().rstrip("/") if provider == "openai" else None,
            "stream": streams if provider == "openai" else None}


def _venv_paths(env: Dict[str, str]) -> None:
    venv_bin = REPO / ".venv" / "bin"
    if venv_bin.exists():
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(REPO / ".venv")
    env["PYTHONPATH"] = f"{REPO / 'src'}:{env.get('PYTHONPATH', '')}"


def ensure_workspace() -> Path:
    if not (WS / ".mcp.json").exists():
        from ..workspace import bootstrap
        bootstrap(WS)
    return WS / ".mcp.json"


# ------------------------------------------------------------------ runner
class Provenance:
    """Append-only provenance of chat turns.

    <root>/turns/<YYYY-MM>/<user-uid>/<session>.jsonl   every record of every turn of one chat:
        turn_start (who, mode, model, app dir, prompt, claude command without secrets),
        stream (raw Claude Code stream-json messages incl. sub-agent ones, partial deltas dropped),
        turn_end (status, answer, cost, tool/agent counts), feedback
    <root>/index.jsonl   one summary line per finished turn (and per feedback) for quick scans.
    """

    lock = threading.Lock()

    def __init__(self, root: Path = None) -> None:
        self.root = Path(root or PROVENANCE)

    def _write(self, path: Path, rec: dict) -> None:
        try:
            line = json.dumps(rec, default=str)
            if len(line) > PROV_MAX_LINE:
                line = json.dumps({k: rec.get(k) for k in ("kind", "ts", "turn_id", "session", "user")}
                                  | {"oversize_bytes": len(line), "head": line[:PROV_MAX_LINE // 2]})
            with self.lock:
                if not self.root.exists():
                    self.root.mkdir(parents=True, exist_ok=True)
                    os.chmod(self.root, 0o700)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as f:
                    f.write(line + "\n")
        except Exception as e:  # provenance must never break a chat
            print(f"provenance write failed: {e}", file=sys.stderr, flush=True)

    def chat_file(self, user: dict, session: dict) -> Path:
        month = time.strftime("%Y-%m", time.localtime(session.get("created") or _now()))
        return self.root / "turns" / month / user["uid"] / f"{session['id']}.jsonl"

    def record(self, user: dict, session: dict, rec: dict) -> None:
        rec = {"ts": _now(), "user": user["name"], "uid": user["uid"], "session": session["id"]} | rec
        self._write(self.chat_file(user, session), rec)

    def index(self, rec: dict) -> None:
        self._write(self.root / "index.jsonl", {"ts": _now()} | rec)


PROV = Provenance()


class _Done:      # stand-in for "no turn": counts as finished
    done = True


class Turn:
    """One running `claude -p` process; events are fanned out to listeners."""

    def __init__(self, session: dict, message: str, store: Store, state: Path, user: dict,
                 files: Optional[list] = None, upload_dir: Optional[Path] = None) -> None:
        self.session = session
        self.store = store
        self.events: list = []
        self.cond = threading.Condition()
        self.done = False
        self.proc: Optional[subprocess.Popen] = None
        claude = claude_bin()
        mode = session.get("mode", "ask")
        tools = ALL_TOOLS
        # Messages go in on stdin (stream-json) so the user can steer a running turn: extra messages
        # are delivered to the agent between tool calls, without interrupting it.
        cmd = [claude, "-p", "--input-format", "stream-json", "--replay-user-messages",
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", session["model"],
               "--mcp-config", str(Path(user["workspace"]) / ".mcp.json"), "--strict-mcp-config",
               "--allowedTools", ",".join(tools),
               "--permission-mode", "acceptEdits",
               "--append-system-prompt", ""]
        from . import prompts
        prompt = prompts.system_prompt(mode, session)
        # approved lessons, read fresh every turn (an approval applies from the next message on)
        chat = "\n".join([session.get("title") or "", session.get("app_dir") or "", message]
                         + [t.get("text") or "" for t in session.get("turns", []) if t.get("role") == "user"][-10:])
        try:
            from ..core import learning
            from ..workspace import sync
            sync(Path(user["workspace"]))         # new or updated capabilities are there for this turn, with no bootstrap
            lessons, self.lessons_meta = learning.context([]), {}
        except Exception as e:  # never block a turn on this
            lessons, self.lessons_meta = "", {"error": str(e)}
        cmd[cmd.index("--append-system-prompt") + 1] = prompt + lessons
        if session.get("claude_session"):
            cmd += ["--resume", session["claude_session"]]
        if session.get("app_dir"):
            cmd += ["--add-dir", session["app_dir"]]
        self.cmd = cmd
        self.env = agent_env(user, session["model"])
        self.env.setdefault("MCP_TIMEOUT", os.environ.get("MOIRAI_AGENTS_MCP_TIMEOUT_MS", "120000"))   # dftracer's server
        self.mcp_reported: Dict[str, bool] = {}            # can need > Claude Code's 30 s default on a slow FS
        self.env["MOIRAI_AGENTS_WEBUI_SESSION"] = session["id"]
        self.env["MOIRAI_AGENTS_USER"] = user["name"]
        self.env["MOIRAI_AGENTS_USER_WORKSPACE"] = user["workspace"]
        if provider_of(user.get("key")) != "login":   # the owner's own login must stay in its own config dir
            self.env["CLAUDE_CONFIG_DIR"] = str(Path(user["workspace"]) / ".claude-home")   # private transcripts
        self.cwd = user["workspace"]
        self.claude_home = self.env.get("CLAUDE_CONFIG_DIR") or str(claude_home())
        self.live_dir = Path(user["workspace"]) / ".moirai-agents" / "live" / uuid.uuid4().hex[:12]
        self.env["MOIRAI_AGENTS_LIVE_DIR"] = str(self.live_dir)     # hooks/live_log.py writes Bash output here
        self.live: Dict[str, dict] = {}                      # running Bash tool_use_id -> {pos, tail}
        # uploads live in the chat's folder of the user's (private) workspace: kept with the chat
        # (MOIRAI_AGENTS_UPLOADS_KEEP=run: a per-run folder deleted when the run ends)
        self.upload_dir = upload_dir or (uploads_dir(user["workspace"], session.get("id", "")) if UPLOADS_KEEP == "chat"
                                         else Path(user["workspace"]) / ".moirai-agents" / "uploads" / uuid.uuid4().hex[:12])
        self.keep_uploads = False          # set when Stop hands the files on to the next run
        self.redact: set = set()           # tool_use ids whose results show upload contents
        meta = self.save_uploads(files or [])
        user_turn = {"role": "user", "text": message, "ts": _now()}
        if meta:
            user_turn["files"] = meta
        session.setdefault("turns", []).append(user_turn)
        self.reply: Dict = {"role": "assistant", "ts": _now(), "events": []}
        session["turns"].append(self.reply)
        self.user = user
        self.turn_id = uuid.uuid4().hex[:12]
        self.started = _now()
        self.tools: Dict[str, int] = {}
        self.agents: list = []
        self.prov = PROV
        self.message = message + self.upload_note(meta)
        self.io_lock = threading.Lock()
        self.sent = 0          # user messages written to the agent's stdin
        self.consumed = 0      # ... and echoed back by it (= picked up by the agent)
        self.stdin_open = False
        self.stopped_by_user = False
        self.steers: list = []      # (message index, text) sent while running
        self.answered = 0           # messages [0, answered) were followed by a result

    def emit(self, ev: dict) -> None:
        if ev.get("type") != "delta":
            ev.setdefault("ts", round(_now(), 3))      # when it happened (server clock): tool start/finish
        with self.cond:
            self.events.append(ev)
            t = ev.get("type")
            if t == "tool_log":                       # live output: streamed, not stored (the result has it)
                lv = self.live.get(ev["id"])
                if lv is not None:
                    lv["tail"] = (lv["tail"] + ev["text"])[-LIVE_TAIL:]
            elif t == "tool_use" and ev.get("name") == "Bash":
                self.live.setdefault(ev["id"], {"pos": 0, "tail": ""})
                self.reply["events"].append(ev)
            elif t == "tool_result":
                self.live.pop(ev.get("id"), None)
                self.reply["events"].append(ev)
            elif t != "delta":
                self.reply["events"].append(ev)
            self.cond.notify_all()

    def _tail_live(self) -> None:
        """Stream the output of running Bash commands (written by the live_log hook) to the UI."""
        while not self.done:
            time.sleep(LIVE_POLL)
            with self.cond:
                running = {k: v["pos"] for k, v in self.live.items()}
            for tid, pos in running.items():
                f = self.live_dir / f"{tid}.log"
                try:
                    with open(f, "rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read(LIVE_CHUNK)
                except OSError:
                    continue
                if not chunk:
                    continue
                with self.cond:
                    if tid not in self.live:
                        continue
                    self.live[tid]["pos"] = pos + len(chunk)
                self.emit({"type": "tool_log", "id": tid, "text": chunk.decode("utf-8", "replace")})
        shutil.rmtree(self.live_dir, ignore_errors=True)

    def save_uploads(self, files: list) -> list:
        """files: [(name, bytes)] -> metadata only ({name, size, sha256}); content goes to upload_dir."""
        meta = []
        for name, data in files:
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.upload_dir, 0o700)
            base = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name or "file"))[:120].strip(" .") or "file"
            p, k = self.upload_dir / base, 1
            while p.exists():
                k += 1
                p = self.upload_dir / f"{Path(base).stem}-{k}{Path(base).suffix}"
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            m = {"name": p.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16], "path": str(p)}
            if p.suffix.lower() == ".pdf":
                m.update(_pdf_text(p))
            meta.append(m)
        return meta

    @staticmethod
    def upload_note(meta: list) -> str:
        if not meta:
            return ""
        def line(m):
            s = f"- {m['path']} ({m['size']:,} bytes)"
            if m.get("text"):
                s += (f"\n  PDF, {m.get('pages', '?')} pages; extracted text: {m['text']} ({m['text_chars']:,} chars). "
                      "Use the text: Grep it, then Read the parts you need with offset/limit. Read the PDF itself "
                      "only for a figure or table, with `pages` (a few at a time) - whole PDFs are sent as page "
                      "images and can overflow the context.")
            return s
        if UPLOADS_KEEP == "chat":
            head = ("\n\n[Uploaded files — kept privately in this chat's uploads folder (only this user; deleted with "
                    "the chat). Read them from there in this and later turns; never copy or commit them anywhere "
                    "shared. Do not load large files whole: search first, then read the relevant parts:]\n")
        else:
            head = ("\n\n[Uploaded files — temporary: they are deleted when this run ends and must not be copied "
                    "or committed anywhere. Process them now and keep only what the user asks for. Do not load large "
                    "files whole: search first, then read the relevant parts:]\n")
        return head + "\n".join(line(m) for m in meta)

    def _uploads_in(self, obj) -> bool:
        return self.upload_dir.exists() and str(self.upload_dir) in json.dumps(obj, default=str)

    def _redact_msg(self, msg: dict) -> dict:
        """Copy of a stream message with tool results that show uploaded content replaced."""
        if msg.get("type") == "assistant":
            for b in (msg.get("message") or {}).get("content") or []:
                if (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") not in ("Task", "Agent")
                        and self._uploads_in(b.get("input"))):
                    self.redact.add(b.get("id"))
            return msg
        if msg.get("type") != "user" or not self.redact:
            return msg
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            return msg
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in self.redact:
                b = dict(b, content=UPLOAD_REDACTED)
            out.append(b)
        return dict(msg, message=dict(msg["message"], content=out))

    def _scrub_transcripts(self) -> int:
        """Claude Code's own session transcripts (for --resume) keep tool results too: replace the ones
        that showed uploaded content, so the content is gone once the run is over."""
        if not self.redact:
            return 0
        home = Path(self.env.get("CLAUDE_CONFIG_DIR") or claude_home()) / "projects"
        n = 0
        for f in home.rglob("*.jsonl") if home.is_dir() else []:
            try:
                if f.stat().st_mtime < self.started - 5:
                    continue
                lines, changed = f.read_text().splitlines(), False
            except OSError:
                continue
            for i, line in enumerate(lines):
                if not any(r and r in line for r in self.redact):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                hit = False
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in self.redact:
                        b["content"], hit = UPLOAD_REDACTED, True
                if hit:
                    rec.pop("toolUseResult", None)
                    lines[i], changed = json.dumps(rec), True
                    n += 1
            if changed:
                tmp = f.with_suffix(".redact.tmp")
                tmp.write_text("\n".join(lines) + "\n")
                os.replace(tmp, f)
        return n

    def drop_uploads(self) -> None:
        if not self.keep_uploads and UPLOADS_KEEP == "run":     # kept with the chat otherwise
            shutil.rmtree(self.upload_dir, ignore_errors=True)

    def start(self) -> None:
        s = self.session
        self.prov.record(self.user, s, {
            "kind": "turn_start", "turn_id": self.turn_id, "turn_index": len(s["turns"]) - 1,
            "title": s.get("title"), "mode": s.get("mode"), "model": s.get("model"),
            "app_dir": s.get("app_dir"), "workspace": self.user["workspace"], "owner": self.user.get("owner"),
            "resume": s.get("claude_session"), "message": s["turns"][-2]["text"],
            "files": [{k: f[k] for k in ("name", "size", "sha256")} for f in s["turns"][-2].get("files", [])],
            "lessons_injected": self.lessons_meta,
            "cmd": [c for c in self.cmd]})
        threading.Thread(target=self._run, daemon=True).start()

    def _write_user(self, text: str) -> bool:
        """Caller holds io_lock."""
        try:
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.proc.stdin.flush()
            self.sent += 1
            return True
        except (OSError, ValueError):
            self.stdin_open = False
            return False

    def _close_stdin(self) -> None:
        """Caller holds io_lock."""
        if self.stdin_open:
            self.stdin_open = False
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def steer(self, text: str, files: Optional[list] = None) -> bool:
        """Hand a message to the running agent without interrupting it. False when the turn has
        already finished taking input (the caller then starts a new turn)."""
        with self.io_lock:
            if not self.stdin_open or self.done or not self.proc or self.proc.poll() is not None:
                return False
            meta = self.save_uploads(files or [])
            if not self._write_user("[Feedback while you were working] " + text + self.upload_note(meta)):
                return False
        self.steers.append((self.sent - 1, text + self.upload_note(meta)))
        self.emit({"type": "steer", "text": text, "n": self.sent - 1, "ts": _now(),
                   "files": [{k: m[k] for k in ("name", "size")} for m in meta]})
        self.prov.record(self.user, self.session, {"kind": "steer", "turn_id": self.turn_id, "message": text})
        return True

    def snapshot(self) -> dict:
        """The session as it is right now (incl. the running reply) + where its live stream is."""
        with self.cond:
            s = json.loads(json.dumps(self.session))
            s["running"] = not self.done
            s["stream_pos"] = len(self.events)
            s["live_logs"] = {k: v["tail"] for k, v in self.live.items() if v["tail"]}
        return s

    def _provenance_stream(self, msg: dict) -> None:
        if msg.get("type") == "stream_event":       # token deltas: the full messages follow anyway
            return
        if msg.get("type") == "assistant":
            for b in (msg.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self.tools[b.get("name")] = self.tools.get(b.get("name"), 0) + 1
                    if b.get("name") in ("Task", "Agent"):
                        inp = b.get("input") or {}
                        self.agents.append({"type": inp.get("subagent_type"),
                                            "description": inp.get("description")})
        self.prov.record(self.user, self.session, {"kind": "stream", "turn_id": self.turn_id, "msg": msg})

    def _run(self) -> None:
        try:
            threading.Thread(target=self._tail_live, daemon=True).start()
            first, resume = self.message, True
            if self.session.get("claude_session") and self.session.get("claude_home") not in (None, self.claude_home):
                reason = "moved"                    # sign-in changed (e.g. LivAI -> Claude login): can't resume
            else:
                reason = None
            wlog("turn-start", user=self.user["name"], chat=self.session["id"], mode=self.session.get("mode"),
                 provider=self.user.get("provider"), model=self.session.get("model"), claude=self.cmd[0])
            if self.user.get("provider") == "openai":
                try:
                    if not BRIDGES.running(self.user["key"], self.user.get("base_url") or OPENAI_URL,
                                           self.user.get("stream", True) is not False):
                        self.emit({"type": "notice", "text": "Starting your OpenAI bridge (it was not running; "
                                                             "up to a minute)…"})
                    b = user_bridge(self.user)
                except RuntimeError as e:
                    wlog("bridge-failed", user=self.user["name"], chat=self.session["id"], error=str(e)[:400])
                    self.emit({"type": "error", "text": f"OpenAI: {e}"})
                    return
                wlog("bridge", user=self.user["name"], chat=self.session["id"], url=b["url"])
                self.env.update(ANTHROPIC_BASE_URL=b["url"], ANTHROPIC_AUTH_TOKEN=b["token"])
            for attempt in range(3):
                if reason:
                    # Continue in a fresh Claude session seeded with a summary of this chat, and redo the
                    # user's request there.
                    self.session.pop("claude_session", None)
                    self.emit({"type": "notice", "text": RESET_NOTICES[reason]})
                    self.prov.record(self.user, self.session, {"kind": "context_reset", "reason": reason,
                                                               "turn_id": self.turn_id})
                    first, resume = self.handoff(), False
                    self.steers = []                # folded into the handoff message
                reason = self._attempt(first, resume=resume)
                if not reason or not resume or self.stopped_by_user:
                    break
        except Exception as e:  # pragma: no cover
            self.emit({"type": "error", "text": f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"})
        finally:
            self.session["updated"] = _now()
            self.store.put(self.session)
            self._provenance_end()
            try:
                self._scrub_transcripts()
            except Exception as e:  # pragma: no cover
                print(f"transcript scrub failed: {e}", file=sys.stderr, flush=True)
            self.drop_uploads()
            with self.cond:
                self.done = True
                self.cond.notify_all()

    def _attempt(self, first: str, resume: bool) -> Optional[str]:
        """One claude process for this turn. Returns why a fresh session is needed ("overflow": the prompt
        got too long; "missing": the session to resume does not exist with this sign-in), else None."""
        cmd = list(self.cmd)
        if not resume and "--resume" in cmd:
            i = cmd.index("--resume")
            del cmd[i:i + 2]
        self.sent = self.consumed = self.answered = 0
        overflow = False
        self.proc = subprocess.Popen(cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     bufsize=1, start_new_session=True)
        if REGISTRY is not None:
            REGISTRY.turn_started(self.proc.pid, self.session["id"], self.user["name"])
        with self.io_lock:
            self.stdin_open = True
            self._write_user(first)
        err_lines: list = []
        threading.Thread(target=lambda: err_lines.extend(self.proc.stderr), daemon=True).start()
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.emit({"type": "log", "text": line[:2000]})
                self.prov.record(self.user, self.session, {"kind": "log", "turn_id": self.turn_id,
                                                           "text": line[:20000]})
                continue
            msg = self._redact_msg(msg)       # uploaded content never reaches chats/provenance
            self._provenance_stream(msg)
            if (msg.get("type") == "user" and not msg.get("parent_tool_use_id")
                    and isinstance((msg.get("message") or {}).get("content"), str)):
                self.consumed += 1                      # our own message, echoed: the agent has it
                if self.consumed > 1:
                    self.emit({"type": "steer_ack", "n": self.consumed - 1})
            if msg.get("type") == "result":
                self.answered = self.consumed
                with self.io_lock:                      # all input handled -> let claude exit
                    if self.consumed >= self.sent:
                        self._close_stdin()
            if msg.get("type") == "result" and msg.get("is_error") and _too_long(msg.get("result")):
                overflow = True                         # handled by the caller (fresh session)
                continue
            if msg.get("type") == "assistant" and _too_long(json.dumps((msg.get("message") or {}).get("content"))[:200]):
                continue
            for ev in normalize(msg):
                if ev["type"] == "init" and ev.get("session_id"):
                    self.session["claude_session"] = ev["session_id"]
                    self.session["claude_home"] = self.claude_home      # where that session is stored
                if ev["type"] == "init":
                    st = {m.get("name"): m.get("status") for m in ev.get("mcp_servers") or []}
                    wlog("mcp", user=self.user["name"], chat=self.session["id"], servers=st, tools=ev.get("tools"))
                    for name, status in st.items():
                        if status not in ("connected", "pending") and not self.mcp_reported.get(name):
                            self.mcp_reported[name] = True
                            why = mcp_failure(self.cwd, name)
                            wlog("mcp-failed", user=self.user["name"], chat=self.session["id"], server=name,
                                 status=status, reason=why["reason"][:300], log=why["log"])
                            self.emit({"type": "notice", "text": f"MCP server '{name}' {status}: {why['reason'][:400]}"
                                       + (f"\nLog (server account): {why['log']}" if why["log"] else "")
                                       + f"\nIts tools are unavailable in this chat; it starts again with the next "
                                         f"message. Start-up limit: MCP_TIMEOUT={self.env.get('MCP_TIMEOUT')} ms."})
                if ev["type"] == "result" and ev.get("session_id"):
                    self.session["claude_session"] = ev["session_id"]
                self.emit(ev)
        with self.io_lock:
            self._close_stdin()
        rc = self.proc.wait()
        if REGISTRY is not None:
            REGISTRY.turn_ended(self.proc.pid)
        tail = "".join(err_lines[-30:])[-4000:]
        overflow = overflow or (rc != 0 and _too_long(tail))
        missing = rc != 0 and resume and "no conversation found" in tail.lower()
        if self.stopped_by_user:
            self.emit({"type": "log", "text": "stopped by user"})
        elif rc != 0 and not overflow and not missing:
            self.emit({"type": "error", "text": f"claude exited with code {rc}\n{tail}"})
        return "overflow" if overflow else "missing" if missing else None

    def handoff(self, max_chars: int = 24000) -> str:
        """First message of a fresh session: this chat so far (requests + final answers), then the
        current request (with its attachments and any messages the user sent meanwhile)."""
        parts, turns = [], self.session["turns"][:-2]
        for t in turns:
            if t.get("role") == "user":
                parts.append("USER: " + (t.get("text") or "")[:1500])
            else:
                evs = t.get("events") or []
                ans = next((e.get("text") for e in reversed(evs) if e.get("type") == "result" and e.get("text")), None) \
                    or next((e.get("text") for e in reversed(evs) if e.get("type") == "text"), "")
                parts.append("ASSISTANT: " + (ans or "(no answer)")[:2500])
        hist = "\n\n".join(parts)
        if len(hist) > max_chars:
            hist = "[... earlier part of the chat omitted ...]\n\n" + hist[-max_chars:]
        pending = "".join(f"\n\n[Message the user sent while you were working]: {t}" for _, t in self.steers)
        return ("[This chat was moved to a fresh session because it outgrew the model's context window. "
                "Below is the conversation so far (user requests and your final answers; tool outputs are "
                "not included - re-run tools or re-read files as needed, reading large files in parts).]\n\n"
                + hist + "\n\n[Current request]\n" + self.message + pending)

    def _provenance_end(self) -> None:
        evs = self.reply["events"]
        res = next((e for e in reversed(evs) if e.get("type") == "result"), {})
        errs = [e.get("text", "")[:2000] for e in evs if e.get("type") == "error"]
        stopped = any(e.get("type") == "log" and "interrupted" in (e.get("text") or "") for e in evs)
        status = ("error" if errs or res.get("is_error") else "stopped" if stopped or not res else "ok")
        summary = {"turn_id": self.turn_id, "turn_index": len(self.session["turns"]) - 1, "status": status,
                   "answer": res.get("text"), "errors": errs, "cost_usd": res.get("cost_usd"),
                   "duration_s": round(_now() - self.started, 1), "num_turns": res.get("turns"),
                   "tools": self.tools, "agents": self.agents,
                   "returncode": self.proc.returncode if self.proc else None}
        self.prov.record(self.user, self.session, {"kind": "turn_end"} | summary)
        wlog("turn-end", user=self.user["name"], chat=self.session["id"], status=status, secs=summary["duration_s"],
             turns=summary["num_turns"], rc=summary["returncode"], error=(errs[-1][:300] if errs else None))
        self.prov.index({"kind": "turn", "user": self.user["name"], "uid": self.user["uid"],
                         "session": self.session["id"], "title": self.session.get("title"),
                         "mode": self.session.get("mode"), "model": self.session.get("model"),
                         "message": self.session["turns"][-2]["text"][:2000],
                         "file": str(self.prov.chat_file(self.user, self.session).relative_to(self.prov.root))}
                        | summary | {"answer": (res.get("text") or "")[:4000]})

    def unanswered(self) -> list:
        """Steer messages the agent never answered (still queued, or received but stopped before acting)."""
        return [t for n, t in self.steers if n >= self.answered]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.stopped_by_user = True
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


_DESC_KEYS = ("description", "question", "title", "query", "q", "pattern", "run_uid", "trace_path",
              "path", "file_path", "url", "name", "benchmark", "storage", "target", "lesson")


def describe(name: str, inp: dict, limit: int = 120) -> str:
    """One readable line (<= limit chars) saying what a tool call does."""
    def clip(t):
        t = " ".join(str(t).split())
        return t if len(t) <= limit else t[:limit - 1] + "…"
    inp = inp if isinstance(inp, dict) else {}
    short = lambda p: str(p).replace(os.path.expanduser("~"), "~")   # noqa: E731
    if name == "Bash":
        return clip(inp.get("description") or ("$ " + str(inp.get("command", ""))))
    if name in ("Task", "Agent"):
        return clip(f"{inp.get('subagent_type') or 'sub-agent'}: {inp.get('description') or ''}")
    if name == "Read":
        rng = f" (from line {inp['offset']})" if inp.get("offset") else ""
        return clip(f"Read {short(inp.get('file_path', ''))}{rng}")
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return clip(f"Edit {short(inp.get('file_path') or inp.get('notebook_path', ''))}")
    if name == "Write":
        n = str(inp.get("content", "")).count("\n") + 1
        return clip(f"Write {short(inp.get('file_path', ''))} ({n} lines)")
    if name == "Grep":
        return clip(f"Search for “{inp.get('pattern', '')}” in {short(inp.get('path') or inp.get('glob') or '.')}")
    if name == "Glob":
        return clip(f"Find files {inp.get('pattern', '')} in {short(inp.get('path') or '.')}")
    if name == "WebFetch":
        return clip(f"Fetch {inp.get('url', '')}")
    if name == "WebSearch":
        return clip(f"Web search: {inp.get('query', '')}")
    if name == "TodoWrite":
        return clip(f"Update the task list ({len(inp.get('todos') or [])} items)")
    if name == "Skill":
        return clip(f"Load skill {inp.get('skill', '')}")
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        what = tool.replace("_", " ")
        args = [f"{k}={inp[k]}" for k in _DESC_KEYS if inp.get(k) not in (None, "", [], {})][:2]
        if not args:
            args = [f"{k}={v}" for k, v in inp.items() if isinstance(v, (str, int, float))][:2]
        return clip(f"{server}: {what}" + (f" ({', '.join(args)})" if args else ""))
    for k in _DESC_KEYS:
        if inp.get(k):
            return clip(f"{name}: {inp[k]}")
    return clip(name)


def _trim(v, limit: int = 6000):
    """Shorten long strings inside a tool input but keep it valid JSON (the UI formats it)."""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"\n… [{len(v) - limit} more characters]"
    if isinstance(v, list):
        return [_trim(x, limit) for x in v[:200]]
    if isinstance(v, dict):
        return {k: _trim(x, limit) for k, x in v.items()}
    return v


def _pdf_text(pdf: Path) -> dict:
    """Extract an uploaded PDF's text next to it (same temporary folder) so the agent can grep and read
    parts instead of loading the whole document (sent to the model as page images)."""
    out = pdf.with_suffix(pdf.suffix + ".txt")
    try:
        subprocess.run(["pdftotext", "-layout", str(pdf), str(out)], capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        try:
            from pypdf import PdfReader
            out.write_text("\n\f".join((pg.extract_text() or "") for pg in PdfReader(str(pdf)).pages))
        except Exception:
            return {}
    try:
        text = out.read_text(errors="replace")
    except OSError:
        return {}
    os.chmod(out, 0o600)
    return {"text": str(out), "text_chars": len(text), "pages": text.count("\f") + 1}


RESET_NOTICES = {
    "overflow": "This chat became longer than the model's context window. Continuing in a fresh session that "
                "starts from a summary of the conversation so far (earlier tool outputs are not carried over).",
    "missing": "The earlier Claude session of this chat is not available with your current sign-in. Continuing "
               "in a fresh session that starts from a summary of the conversation so far.",
    "moved": "You are signed in differently than when this chat started (e.g. LivAI -> Claude subscription), so "
             "its earlier Claude session cannot be resumed. Continuing in a fresh session that starts from a "
             "summary of the conversation so far.",
}


def _too_long(text) -> bool:
    t = str(text or "").lower()
    return "prompt is too long" in t or "context window" in t and "exceed" in t or "prompt_too_long" in t


def normalize(msg: dict):
    """Claude Code stream-json -> compact UI events."""
    t = msg.get("type")
    if t == "system" and msg.get("subtype") == "init":
        yield {"type": "init", "session_id": msg.get("session_id"), "model": msg.get("model"),
               "mcp_servers": msg.get("mcp_servers"), "tools": len(msg.get("tools") or [])}
    elif t == "stream_event":
        ev = msg.get("event") or {}
        d = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
            yield {"type": "delta", "text": d.get("text", "")}
    elif t == "assistant":
        parent = msg.get("parent_tool_use_id")
        for b in (msg.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b.get("text"):
                yield {"type": "text", "text": b["text"], "parent": parent}
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                yield {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "parent": parent,
                       "summary": describe(b.get("name") or "", inp),
                       "input": json.dumps(_trim(inp), indent=1),
                       "agent": ({"description": inp.get("description"), "type": inp.get("subagent_type")}
                                 if b.get("name") in ("Task", "Agent") else None)}
    elif t == "user":
        for b in (msg.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                yield {"type": "tool_result", "id": b.get("tool_use_id"), "is_error": b.get("is_error"),
                       "text": str(c)[:8000], "parent": msg.get("parent_tool_use_id")}
    elif t == "result":
        yield {"type": "result", "session_id": msg.get("session_id"), "is_error": msg.get("is_error"),
               "text": msg.get("result"), "cost_usd": msg.get("total_cost_usd"),
               "duration_ms": msg.get("duration_ms"), "turns": msg.get("num_turns")}


# ------------------------------------------------------------------ http
class App:
    def __init__(self, token: str, state: Path) -> None:
        self.token = token
        self.state = state
        state.mkdir(parents=True, exist_ok=True)

        self.user: Optional[dict] = None        # the one person this server belongs to
        self.stores: Dict[str, Store] = {}
        self.turns: Dict[str, Turn] = {}
        self.lock = threading.Lock()
        global BRIDGES
        self.bridges = BRIDGES = Bridges(state, REPO)

    def store(self, user: dict) -> Store:
        ws = user["workspace"]
        if ws not in self.stores:
            self.stores[ws] = Store(Path(ws) / ".moirai-agents")   # chats live in the user's workspace
        return self.stores[ws]

    def local_user(self) -> dict:
        """Whoever started the server: there are no accounts."""
        if self.user is None:
            ws = str(WS.resolve())
            prepare_workspace(ws)
            self.user = {"name": os.environ.get("USER", "me"), "uid": "local", "owner": True, "created": _now(), "workspace": ws,
                         "model_path": str(owner_model_config().path)}
            resolve_model(self.user)
            self.store(self.user)
            prewarm_bridge(self.user)
        return self.user

    def refresh_models(self) -> None:
        """After a model-settings change."""
        resolve_model(self.local_user())
        prewarm_bridge(self.user)


OWNER_DIR = Path(os.environ.get("MOIRAI_AGENTS_OWNER_DIR", str(STATE / "owner")))   # the owner's own settings folder


def resolve_model(user: dict) -> dict:
    prov, e = ModelConfig(Path(user["model_path"])).active()
    user.update(provider=prov, key=(e or {}).get("key"), base_url=(e or {}).get("base_url"), stream=(e or {}).get("stream"),
                cred=(e or {}).get("cred"), models=(e or {}).get("models") or [],
                model=(e or {}).get("model"))
    return user


def owner_model_config() -> ModelConfig:
    """The owner's model settings live in OWNER_DIR. First use: start from the .env key, as before."""
    mc = ModelConfig(OWNER_DIR / "model.json")
    if not mc.path.exists() and server_key():
        key = server_key()
        prov = "livai" if provider_of(key) == "livai" else "anthropic"
        models = provider_models(provider_of(key))
        mc.put(prov, {"key": key, "cred": provider_of(key) if prov == "anthropic" else prov, "models": models,
                      "model": os.environ.get("MOIRAI_AGENTS_MODEL") if os.environ.get("MOIRAI_AGENTS_MODEL") in models else models[0],
                      "verified": None, "from": ".env"})
    return mc


def send_to_session(app: "App", user: dict, sid: str, msg: str) -> bool:
    """Deliver a message to a chat the way /api/chat does: into the running turn if there is one, otherwise as the next turn (resuming the session)."""
    st = app.store(user)
    key = f"{user['uid']}/{sid}"
    old = app.turns.get(key)
    if old and not old.done:
        if old.steer(msg):
            return True
        with old.cond:
            old.cond.wait_for(lambda: old.done, timeout=60)
    s = st.get(sid)
    if not s:
        return False
    t = Turn(s, msg, st, app.state, user)
    app.turns[key] = t
    st.put(s)
    t.start()
    return True


def touch_activity(ws: str) -> None:
    p = Path(ws) / ".moirai-agents" / "last_active"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(_now()))
    except OSError:
        pass


def stable_token(state: Path) -> str:
    """The private URL's token, kept across restarts so an open tab keeps working (<state>/owner_token, mode 600)."""
    p = state / "owner_token"
    try:
        tok = p.read_text().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(18)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


def prepare_workspace(ws: str) -> Path:
    """Bootstrap agent files (CLAUDE.md, .mcp.json, skills, agents, settings) once per workspace."""
    marker = Path(ws) / ".moirai-agents" / "workspace.json"
    from ..workspace import WORKSPACE_VERSION, bootstrap
    try:
        ver = json.loads(marker.read_text()).get("version") if marker.exists() else None
    except ValueError:
        ver = None
    if not (Path(ws) / ".mcp.json").exists() or ver != WORKSPACE_VERSION:
        bootstrap(Path(ws))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"bootstrapped": _now(), "version": WORKSPACE_VERSION}))
    for d in ("apps", ".claude-home", ".moirai-agents"):
        (Path(ws) / d).mkdir(exist_ok=True)
    return Path(ws) / ".mcp.json"


