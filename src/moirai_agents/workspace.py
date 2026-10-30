"""Render the Claude Code workspace from the capability store.

    CLAUDE.md, .mcp.json, .claude/settings.json          the harness wiring
    .claude/skills/<capability>  -> the capability's folder (a link: a changed skill is live at once)
    .claude/agents/<capability>.md                       rendered from agent.json: its fixed scope

The harness rules come from dftracer-agents, installed editable: its skills and agents are installed with its own APIs, the permission
tiers are rendered from its policy (``workspaces/**`` is auto-allowed, so sessions and pipelines both live there), and its MCP server sits next to ours.

Agents declare a level, never a model, so they run on whatever the chat runs on (Claude, or OpenAI and LivAI models through
the same harness). :func:`sync` is idempotent and cheap: it runs before every chat turn, so a new or updated capability
needs no bootstrap.

    python3 -m moirai_agents.workspace [--path DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .core import capstore

PKG = Path(__file__).resolve().parent
REPO = PKG.parents[1]
DEFAULT_WS = REPO / "workspace"
HOOKS = PKG / ".agents" / "hooks"
WORKSPACE_VERSION = 2

CLAUDE_MD = """# moirai-agents workspace

You help people capture and explore **data provenance with moirai**. Everything here is a **capability**: an MCP tool
(what is deterministic), a skill (rules, constraints, corner cases, how to self-heal and self-learn) and a background agent with
a fixed scope. Find them with `capability_list` and `lesson_search`.

Two pipelines, each a list of stages run with the `pipeline_*` tools, one capability per stage:

- **run**: explore -> smoke -> annotate -> integrate -> moirai_smoke -> baseline -> query
- **analysis**: ingest -> dashboard -> query

For every stage:

1. Load the owner's skill and read its LESSONS.md. Delegate to its agent when the work is long.
2. Run its predictor tool, then its validator tool. A failed verdict goes to the skill's Corrector section: repair the original tool or its input, then predict and validate again. The same failure twice stops the loop.
3. Stop at the gate: show what was produced and call `ask_user`, then end your turn. Never advance past a gate on your own.
4. The answer comes back as a message. `process` feedback changes how the work is done: `capability_get`, then `capability_update`
   or `capability_new`; the user approves it in the Capabilities panel. `remember` feedback is already a lesson.
5. After the user approved, `pipeline_decide` then `pipeline_advance`.

This builds on **dftracer-agents**: its skills (`system-detect`, `dftracer-build-app`, `flux-alloc`, `dftracer-trace-utils`, ...), its agents and its `mcp__dftracer__*`
tools are here. Use them instead of rewriting them. All work happens in the session under `workspaces/`: take paths from `session_get_run_paths`, never build them by hand.

Rules
- Deterministic and repeated work becomes a capability, never ad-hoc shell. Use `capability_new`.
- A tool that fails is repaired, not worked around: diagnose, `capability_update`, verify. Never edit capability files directly.
- The user's source tree is read-only: annotation happens on a copy inside the pipeline.
- Builds of large apps and traced runs go to compute nodes through the launcher the user approved (`flux run ...`); never allocate
  more than one node or an hour without asking.
- Never invent results. Every number comes from a tool, with the pipeline id or trace it came from.
"""

SETTINGS = {
    "env": {"REQUESTS_CA_BUNDLE": "/etc/pki/tls/cert.pem", "SSL_CERT_FILE": "/etc/pki/tls/cert.pem"},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {HOOKS / 'live_log.py'}", "timeout": 10}]}]},
    "permissions": {"deny": ["Bash(sudo *)", "Bash(sudo)", "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "Write(/etc/*)", "Write(/var/*)",
                             f"Edit({PKG}/**)", f"Write({PKG}/**)", f"MultiEdit({PKG}/**)",
                             f"Edit({REPO}/capabilities/**)", f"Write({REPO}/capabilities/**)"]},
}


_DFTRACER_SETUP = """
import json, sys
from pathlib import Path
from dftracer_agents.skills import ensure_setup
from dftracer_agents.agents import ensure_agents_setup
from dftracer_agents.bootstrap import ensure_workspace_setup
from dftracer_agents.permissions import discover_mcp_tool_names, load_policy, render_claude_permissions
t = Path(sys.argv[1]); out = {}
for k, fn in (("skills", ensure_setup), ("agents", ensure_agents_setup), ("workspace", ensure_workspace_setup)):
    try: out[k] = fn(target_root=t, force=False).get("status")
    except Exception as e: out[k] = f"error: {e}"
out["permissions"] = render_claude_permissions(load_policy(), discover_mcp_tool_names())
print(json.dumps(out))
"""


def _python() -> str:
    py = REPO / ".venv" / "bin" / "python"
    return str(py if py.exists() else sys.executable)


def dftracer_setup(ws: Path) -> dict:
    """Skills, agents, workspace files and the permission policy, from dftracer-agents' own APIs."""
    r = subprocess.run([_python(), "-c", _DFTRACER_SETUP, str(ws)], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"skipped": (r.stderr or r.stdout)[-400:]}


def inherit_models(ws: Path) -> None:
    """dftracer's agents pin models; here they run on what the chat runs on (Claude, LivAI or OpenAI through the bridge)."""
    for f in (ws / ".claude" / "agents").glob("*.md"):
        t = f.read_text()
        t2 = re.sub(r"(?m)^model:\\s*\\S+\\s*$", "model: inherit", t, count=1)
        if t2 != t:
            f.write_text(t2)


def mcp_servers() -> dict:
    servers = {"moirai": {"command": _python(), "args": ["-m", "moirai_agents.mcp_server"], "env": {"PYTHONPATH": str(REPO / "src")}}}
    dft = REPO / ".venv" / "bin" / "dftracer-mcp-server"
    if os.environ.get("MOIRAI_AGENTS_DFTRACER_MCP_URL"):
        servers["dftracer"] = {"type": "http", "url": os.environ["MOIRAI_AGENTS_DFTRACER_MCP_URL"]}
    elif dft.exists():
        servers["dftracer"] = {"command": str(dft), "args": ["run", "--transport", "stdio", "--skip-setup"], "env": {"PATH": f"{dft.parent}:/usr/bin:/bin"}}
    return servers


def render_agent(name: str, agent: dict, tools: list) -> str:
    mcp = [f"mcp__moirai__{t}" for t in list(tools) + capstore.SHARED_AGENT_TOOLS]
    scope = list(dict.fromkeys(agent.get("tools", []) + mcp))
    head = ["---", f"name: {name}", f"description: {json.dumps(agent.get('description', ''))}", f"tools: {', '.join(scope)}",
            "model: inherit", f"skills: {', '.join(agent.get('skills', [name]))}", f"permissionMode: {agent.get('permission_mode', 'default')}",
            f"background: {'true' if agent.get('background', True) else 'false'}", "---", ""]
    body = agent.get("instructions") or f"You run capability {name} and nothing else."
    return "\n".join(head) + body + "\n\nThis is your whole scope. If the work needs something outside it, say so and stop.\n"


def sync(ws: Path) -> dict:
    skills, agents = ws / ".claude" / "skills", ws / ".claude" / "agents"
    skills.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    caps = {n: v for n, v in capstore.all_caps().items() if v[2].get("enabled", True)}
    for name, (_, d, meta) in caps.items():
        link = skills / name
        if not link.is_symlink() or os.readlink(link) != str(d):
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(d)
        try:
            agent = json.loads((d / "agent.json").read_text())
        except (OSError, ValueError):
            continue
        text, f = render_agent(name, agent, meta.get("tools", [])), agents / f"{name}.md"
        if not f.exists() or f.read_text() != text:
            f.write_text(text)
    for p in list(agents.glob("*.md")):              # a disabled capability; dftracer's agents have other names
        if p.stem in capstore.all_caps() and p.stem not in caps:
            p.unlink()
    for p in list(skills.iterdir()):
        if p.is_symlink() and p.stem in capstore.all_caps() and p.name not in caps:
            p.unlink()
    return {"capabilities": sorted(caps)}


def bootstrap(ws: Path = DEFAULT_WS) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "workspaces").mkdir(exist_ok=True)
    df = dftracer_setup(ws)
    for name in ("CLAUDE.md", ".mcp.json", ".claude/settings.json"):     # dftracer links its own here: this workspace owns them
        p = ws / name
        if p.is_symlink() or p.exists():
            p.unlink()
    (ws / "CLAUDE.md").write_text(CLAUDE_MD)
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": mcp_servers()}, indent=1) + "\n")
    (ws / ".claude").mkdir(exist_ok=True)
    settings = json.loads(json.dumps(SETTINGS))
    perms = df.pop("permissions", None)
    if perms:
        settings["permissions"]["deny"] = sorted(set(settings["permissions"]["deny"]) | set(perms["deny"]))
        settings["permissions"]["allow"] = perms["allow"]
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings, indent=1) + "\n")
    hooks = ws / ".claude" / "hooks"
    if hooks.is_symlink() and "dftracer_agents" in os.readlink(hooks):     # they reference files of its own projects
        hooks.unlink()
    inherit_models(ws)
    return {"workspace": str(ws), "dftracer": df, **sync(ws)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(DEFAULT_WS))
    print(json.dumps(bootstrap(Path(ap.parse_args(argv).path)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
