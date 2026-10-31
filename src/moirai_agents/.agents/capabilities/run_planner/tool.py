"""Plan first, and keep the plan true. The planner writes what each stage will do and how it will be judged; the engine then syncs each stage's status and
result into the plan as it runs, and a failed verdict or process feedback marks the step stale until the plan is revised."""
import json

from moirai_agents.core import pipeline as P

STAGE = "plan"


def register(mcp):
    @mcp.tool()
    def plan_write(pipeline: str, goal: str, steps: dict, assumptions: list = None, questions: list = None) -> dict:
        """Write the plan. Use first in every pipeline, after reading the app or traces, and before any stage runs. goal: what the user wants from this run, in their terms. steps: one
        entry per stage, {stage: {intent, approach, done_when, risks}}. assumptions and questions: what you are unsure of. Rewrites the plan; to change it later use plan_update.
        Returns {status, stages, file} or {status: error, problems}."""
        steps = json.loads(steps) if isinstance(steps, str) else steps
        c = P.load(pipeline)
        order = P.KINDS[c["kind"]]
        bad = [f"no step for stage {s}" for s in order if s not in steps]
        bad += [f"{s} is not a stage of a {c['kind']} pipeline" for s in steps if s not in order]
        bad += [f"step {s} needs intent and done_when" for s, d in steps.items() if not (d.get("intent") and d.get("done_when"))]
        if not goal.strip():
            bad.append("a plan needs its goal")
        if bad:
            return {"status": "error", "problems": bad}
        P.write_plan(pipeline, goal, steps, assumptions or [], questions or [])
        return {"status": "ok", "stages": order, "file": str(P.work(c) / "PLAN.md")}

    @mcp.tool()
    def plan_update(pipeline: str, stage: str, why: str, intent: str = "", approach: str = "", done_when: str = "", risks: str = "") -> dict:
        """Revise a step. Use whenever reality differs from the plan: a failed verdict, process feedback, a changed scale or approach. It clears the stale mark, which a stage cannot pass with.
        why: what changed and what the plan now does. Other fields replace that part of the step. Returns {status, stage, revisions}."""
        return P.update_plan(pipeline, stage, why, {k: v for k, v in (("intent", intent), ("approach", approach), ("done_when", done_when), ("risks", risks)) if v})

    @mcp.tool()
    def plan_show(pipeline: str) -> dict:
        """The plan as it stands: each step with its live status, verdict, result and whether it is stale. Use at the start of a turn, and to brief an agent on its stage.
        Returns {goal, steps, assumptions, questions, revisions}."""
        return P.load(pipeline).get("plan") or {"status": "error", "error": "no plan yet: plan_write"}

    @mcp.tool()
    def plan_validate(pipeline: str) -> dict:
        """Validate the plan stage: every stage has a step with an intent and a test of done, and the file is written. Use before the gate. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        plan = c.get("plan") or {}
        v = P.Verdict(STAGE)
        if v.check("written", bool(plan), "no plan", "run plan_write"):
            missing = [s for s in P.KINDS[c["kind"]] if s not in plan["steps"]]
            v.check("covers_stages", not missing, f"missing: {missing}", "plan every stage")
            v.check("file", (P.work(c) / "PLAN.md").exists(), "PLAN.md is missing", "run plan_write")
            if plan.get("questions"):
                v.inconclusive("questions", f"{len(plan['questions'])} open: settle them with the user at the gate")
        return P.finish_validation(pipeline, v)
