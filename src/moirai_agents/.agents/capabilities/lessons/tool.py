"""What the system remembers: lessons attached to the capability they concern, and search over capabilities and lessons."""
from moirai_agents.core import capstore as C
from moirai_agents.core import consolidate as Q
from moirai_agents.core import knowledge as K
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
    def lesson_search(query: str, k: int = 6) -> dict:
        """Find the lessons closest to a failure or a question: a graph search over every capability's lessons and the lesson sections of skills, then a match on their words.
        Use with the error text the moment something fails, before exploring on your own. A failed tool's result already carries these as known_lessons.
        Returns {status, results: [{kind, title, source, line} or {kind, id, capability, text}]}; open a graph row with knowledge_read(source, line)."""
        return K.lessons(query, k)

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
    def lesson_revise(id: str, text: str, note: str, capability: str = "") -> dict:
        """Refine a lesson as the user asked. It waits for them in the Lessons panel: never applied by this call. An applied lesson stays in force until they accept
        the correction. text: the whole new lesson. note: what was wrong. capability: only to move it. Returns {status, id}; id is the correction's when the lesson was applied."""
        return L.revise(id, text, capability, note)

    @mcp.tool()
    def consolidation_report() -> dict:
        """What overlaps or contradicts among lessons and capabilities. Use when asked to consolidate. Each group is a candidate, not a verdict: read its members before acting.
        Returns {lessons: [{kind: conflict|overlap|crowded, ids, capabilities, texts}], capabilities: [{kind, ids, texts}], checked}."""
        return Q.report()

    @mcp.tool()
    def lesson_merge(ids: list, text: str, capability: str, note: str) -> dict:
        """Propose one lesson to replace several that overlap. It waits for the user in the Lessons panel, and the old ones stay in force until they accept it. Never use this to settle a contradiction:
        ask the user which holds first. ids: the lessons replaced. text: the whole merged lesson, keeping every fact. capability: its one best home. note: what was merged and why. Returns {status, id, replaces}."""
        return L.merge(ids, text, capability, note)

    @mcp.tool()
    def consolidation_keep(ids: list, why: str) -> dict:
        """Record that a reported group is different on purpose, so it is not reported again. Use after reading its members. ids: the group's ids. why: what makes them distinct. Returns {status, kept}."""
        return Q.keep(ids, why)

    @mcp.tool()
    def lesson_decide(id: str, approve: bool) -> dict:
        """Apply or reject a pending lesson. Use only with the user's decision. Returns {status, id, applied}."""
        return L.decide(id, approve)
