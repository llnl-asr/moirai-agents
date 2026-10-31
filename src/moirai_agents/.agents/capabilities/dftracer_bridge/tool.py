"""Use dftracer-agents from a capability: its session, build and system tools, in this process, refreshed when its code changes."""
import os
from pathlib import Path

from moirai_agents.core import dfbridge as B
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def df_status() -> dict:
        """Whether dftracer-agents is usable here, which code is loaded, and the session root. Use before relying on its tools. Returns {status, available, loaded, package, workspaces}."""
        if not B.available():
            return {"status": "ok", "available": False, "fix": "scripts/setup_env.sh installs dftracer-agents editable"}
        return {**B.status(), "available": True}

    @mcp.tool()
    def df_validate() -> dict:
        """Rule on the bridge: dftracer-agents loads, it publishes the tools the pipelines call, and its session root is writable. Use after df_call fails, or after
        setup. No dftracer-agents here is inconclusive, not a failure. Returns {status, failed, unknown, passed, total}."""
        v = P.Verdict("dftracer_bridge")
        if v.assume("installed", B.available(), "dftracer-agents is not installed"):
            names = set(B.tools())
            need = {"session_create", "session_detect", "session_configure", "session_build_install", "session_run_smoke_test", "session_get_run_paths", "system_detect"}
            v.check("tools", need <= names, f"missing {sorted(need - names)}", "update the dftracer-agents checkout, or the capability that calls the renamed tool")
            root = Path(B.workspaces_root())
            root.mkdir(parents=True, exist_ok=True)
            v.check("session_root", os.access(root, os.W_OK), str(root), "the session root must be writable")
        return v.result()

    @mcp.tool()
    def df_call(tool: str, args: dict = None) -> dict:
        """Call one dftracer-agents tool by name inside this pipeline's session root. Use when a capability has no wrapper for it, e.g. session_get_run_paths, clang_extract_functions. Returns the tool's JSON."""
        return B.call(tool, args)

    @mcp.tool()
    def df_refresh() -> dict:
        """Rebuild the dftracer-agents tools now (they also refresh by themselves when its code changes). Use after installing something it imports. Returns {status, builds, fingerprint}."""
        return B.refresh()
