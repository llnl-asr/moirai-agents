"""Explain a computed answer in four parts - plan, result, answer, evidence - each by its own capability (explain_plan, explain_result,
explain_answer, explain_evidence), so each part is refined and learned on its own. The Query view shows each part's summary and keeps its
details folded. A part that fails is reported, never invented."""
from moirai_agents.core import capstore as S
from moirai_agents.core import explain as X


def _answer(pipeline: str, answer_id: str) -> dict:
    from moirai_agents.core import answers as A
    rec = next((a for a in A.listing(pipeline, limit=10 ** 6) if a.get("id") == answer_id), None)
    if not rec:
        raise ValueError(f"no answer {answer_id} on {pipeline}")
    return rec


def explain(answer: dict) -> dict:
    parts, problems = [], {}
    for name in X.PARTS:
        try:
            p = S.call(f"explain_{name}", f"explain_{name}", answer=answer)
            if not isinstance(p, dict) or p.get("status") == "error":
                raise ValueError((p or {}).get("error") or "no explanation")
        except Exception as e:                       # one part failing leaves the others
            p = X.part(name, name.capitalize(), f"This part could not be explained: {e}.")
            problems[name] = [str(e)]
        parts.append(p)
    return {"parts": parts, "problems": problems, "versions": {f"explain_{n}": (S.all_caps().get(f"explain_{n}") or (0, 0, {}))[2].get("version") for n in X.PARTS}}


def register(mcp):
    @mcp.tool()
    def query_explain(pipeline: str = "", answer_id: str = "", answer: dict = None) -> dict:
        """Explain a computed answer for the Query view: plan, result, answer and evidence, each a high-level summary then details as typed
        blocks (text, kv, table, query, stats, list, note; see core.explain). Give pipeline and answer_id (an answer in its answers.jsonl), or
        the answer record itself. Returns {parts: [{part, title, summary, facts, details}], problems: {part: [...]}, versions}."""
        return explain(answer if answer else _answer(pipeline, answer_id))

    @mcp.tool()
    def query_explain_validate(pipeline: str = "", answer_id: str = "", answer: dict = None) -> dict:
        """Validator: each of the four parts of an answer's explanation passes its own capability's validator (explain_<part>_check) - with a
        pipeline, against the traces: the plan runs again with TraceViewer and every claim is looked up in what it computes.
        Returns {status: pass|fail, parts: {part: {status, problems, data}}}."""
        a = answer if answer else _answer(pipeline, answer_id)
        out = {}
        for name in X.PARTS:
            try:
                out[name] = S.call(f"explain_{name}", f"explain_{name}_check", answer=a, pipeline=pipeline)
            except Exception as e:
                out[name] = {"status": "fail", "problems": [f"explain_{name}_check failed: {e}"]}
        return {"status": "pass" if all(v.get("status") == "pass" for v in out.values()) else "fail", "parts": out}
