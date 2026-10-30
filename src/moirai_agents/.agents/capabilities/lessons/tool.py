"""What the system remembers: lessons attached to the capability they concern, and search over capabilities and lessons."""
from moirai_agents.core import capstore as C
from moirai_agents.core import learning as L
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def lesson_add(capability: str, text: str, evidence: str, source: str = "agent") -> dict:
        """Record a lesson on a capability. Use when something was diagnosed and verified, or the user stated a rule. source=user: their words,
        applied now. source=agent: pending until the user approves it in the Lessons panel. evidence: run, log line or file:line. A near-duplicate is refused.
        Returns {status, id, capability}."""
        return L.add(capability, text, source, evidence)

    @mcp.tool()
    def lesson_list(status: str = "") -> dict:
        """List lessons, newest first. status: pending|applied|rejected, or all. Use to see what is known or awaiting approval.
        Returns {lessons: [{id, capability, text, status, source}]}."""
        return {"lessons": L.listing(status or None)}

    @mcp.tool()
    def lesson_search(query: str, k: int = 8) -> dict:
        """Find capabilities and lessons that match a question. Use before acting on anything unfamiliar: locating costs little, rediscovering a lot.
        Returns {results: [{kind, name|id, score, ...}]}."""
        return {"results": L.search(query, k)}

    @mcp.tool()
    def lesson_validate() -> dict:
        """Rule on the lesson store: every applied lesson is in its capability's LESSONS.md, its capability exists, an agent's lesson has evidence.
        Use after recording lessons. Returns {status, failed, passed, total}."""
        v = P.Verdict("lessons")
        caps = C.all_caps()
        for r in L.listing():
            if r["status"] != "applied":
                continue
            ex = caps.get(r["capability"])
            if v.check(f"{r['id']}:capability", bool(ex), r["capability"], "the capability is gone: move the lesson or reject it"):
                f = ex[1] / "LESSONS.md"
                v.check(f"{r['id']}:written", f.exists() and r["id"] in f.read_text(), r["text"][:80], "the lesson never reached LESSONS.md: add it again")
            v.check(f"{r['id']}:evidence", r.get("source") == "user" or bool(r.get("evidence")), "no source", "reject it: no source, no lesson")
        return v.result()

    @mcp.tool()
    def lesson_decide(id: str, approve: bool) -> dict:
        """Apply or reject a pending lesson. Use only with the user's decision. Returns {status, id, applied}."""
        return L.decide(id, approve)
