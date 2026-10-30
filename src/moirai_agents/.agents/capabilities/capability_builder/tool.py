"""The capability that builds capabilities: propose, validate, approve, verify and retire a tool + skill + agent."""
import json

from moirai_agents.core import capstore as S


def _pick(agent, checks):
    return (json.loads(agent) if isinstance(agent, str) else agent), (json.loads(checks) if isinstance(checks, str) else checks)


def register(mcp):
    @mcp.tool()
    def capability_list() -> dict:
        """List every capability (active, with version, layer, tools, stages, load error) and the pending proposals.
        Use first, to extend what exists instead of duplicating it. Returns {active: [...], pending: [...]}."""
        return S.list_capabilities()

    @mcp.tool()
    def capability_get(name: str) -> dict:
        """Read one capability whole: code, skill, lessons, agent scope, checks. Use before capability_update.
        Returns {status, name, layer, meta, code, skill, lessons, agent, checks}."""
        return S.get(name)

    @mcp.tool()
    def capability_new(name: str, tool_code: str, skill_md: str, agent: dict, checks: list, description: str, why: str, pvc: dict,
                       stages: list = None, layer: str = "project") -> dict:
        """Propose a new capability. Use when deterministic work has no tool, or feedback calls for a new process.
        tool_code: Python with register(mcp) and documented @mcp.tool() functions. skill_md: frontmatter (name, description) and the sections
        Rules, Constraints, Corner cases, Self-heal, Self-learn. agent: {description, level, tools (scoped built-ins), skills, permission_mode,
        instructions}. checks: [{tool, args, expect: {contains|json|raises}}]. pvc: {predictor: [tools], validator: [tools]}: every capability is a predictor-validator-corrector loop. stages: pipeline stages it serves. layer: project|user.
        Everything is validated and the checks run in a sandbox; nothing is live until a person approves.
        Returns {status, id, tools, review, next} or {status: error, problems}."""
        a, c = _pick(agent, checks)
        return S.propose(name, tool_code, skill_md, a, c, description, why, stages, layer, "new", pvc=pvc)

    @mcp.tool()
    def capability_update(name: str, tool_code: str, skill_md: str, agent: dict, checks: list, why: str, description: str = "", pvc: dict = None) -> dict:
        """Propose a new version of a capability, with a check that reproduces what was wrong. Use for process feedback and to
        repair a failing tool. Same arguments as capability_new. Returns {status, id, diff, next} or {status: error, problems}."""
        a, c = _pick(agent, checks)
        ex = S.all_caps().get(name)
        return S.propose(name, tool_code, skill_md, a, c, description or (ex[2].get("description", "") if ex else ""), why, kind="update", pvc=pvc)

    @mcp.tool()
    def capability_verify(name: str = "", id: str = "") -> dict:
        """Re-run every validator and the sandboxed checks on a capability (name) or a proposal (id). Use when one misbehaves.
        Returns {status, target, problems, warnings, checks}."""
        return S.verify(name, id)

    @mcp.tool()
    def capability_apply(id: str, confirmed: bool = False) -> dict:
        """Activate a proposal. Use only after the user said yes; pass confirmed=true. It goes live with no restart.
        Returns {status, name, version, tools, active}."""
        return S.apply(id, confirmed)

    @mcp.tool()
    def capability_reject(id: str, note: str = "") -> dict:
        """Drop a proposal. Use when the user declined it. Returns {status, id, rejected}."""
        return S.reject(id, note)

    @mcp.tool()
    def capability_rollback(name: str, version: int, confirmed: bool = False) -> dict:
        """Restore an earlier version (recorded as a new one) after the user agreed. Use when an update made things worse.
        Returns {status, name, version, restored_from}."""
        return S.rollback(name, version, confirmed)

    @mcp.tool()
    def capability_disable(name: str) -> dict:
        """Take a capability out of service, keeping it. Use when it is broken and has no quick fix. Returns {status, name, enabled}."""
        return S.disable(name)

    @mcp.tool()
    def capability_enable(name: str) -> dict:
        """Put a disabled capability back. Use after it was repaired. Returns {status, name, enabled}."""
        return S.enable(name)
