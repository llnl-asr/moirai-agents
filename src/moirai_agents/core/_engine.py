"""moirai-agents runs on phronix's capability engine (phronix.engine); this configures it for moirai-agents.

moirai-agents owns the provenance capabilities (prov_*, explain_*, moirai_*, the pipeline and answer capabilities, and
any capability importing moirai_agents); they live in this package, .agents/capabilities. Upstream of it are
dftracer-agents (performance engineering) and phronix (workloads, software, systems, tooling, process), used from
clones in vendor/: moirai-agents loads its own capabilities over theirs, and what it learns about one of theirs is
committed on contrib/moirai-agents in a worktree of that clone (vendor/contrib/<project>), for a person to push and
open the pull request (the web UI's Update button, or python -m phronix.engine update --bootstrap
moirai_agents.core._engine).
"""
from __future__ import annotations

from pathlib import Path

from phronix.engine import configure
from phronix.engine.scopes import Scope, phronix_scope

from ..env import state_dir

PKG = Path(__file__).resolve().parents[1]
NAMES = ("answer_confidence", "query_explain", "repro_package", "live_inputs", "trace_ingest", "chat_pdf",
         "app_explore", "app_smoke", "pipeline_control", "run_planner", "tools_moirai_venv")


def scope() -> Scope:
    return Scope(project="moirai-agents", package="moirai_agents", cap_dir=PKG / ".agents" / "capabilities",
                 prefixes=("prov_", "explain_", "moirai_"), names=NAMES)


def upstream():
    """phronix, then dftracer-agents when it is installed."""
    try:
        from dftracer_agents.capabilities import chain
        return chain()
    except ImportError:
        return [phronix_scope()]


SETTINGS = configure(
    project="moirai-agents", env_prefix="MOIRAI_AGENTS", package_dir=PKG,
    state_dir=state_dir, module_prefix="moirai", context_tag="moirai-context",
    trace_rule="read traces only through TraceViewer (moirai_agents.core.provtrace / evidence, or dftracer.utils.TraceViewer), "
               "never by decompressing them",
    trace_reader="prov_read (moirai_agents.core.provtrace: read, row_counts, ensure_index, tv)",
    bootstrap="moirai_agents.core._engine", scope=scope(), upstream=upstream())

