"""Explain the plan of a computed answer: what was asked of the traces, step by step - first one plain sentence, then per step what it
asked (the arguments, the time it resolved to), the TraceViewer queries it ran and what it found. Blocks: see core.explain."""
import re

from moirai_agents.core import explain as X


def _what(s: dict) -> str:
    """One step in words, from its op and arguments."""
    a = s.get("args") or {}
    op = s.get("op", "")
    frm = f"S{int(a['from']) + 1}" if isinstance(a.get("from"), int) else ""
    if op == "entities":
        what = f"find the {a['type']} entities" if a.get("type") else "find entities"
        if a.get("uri_contains"):
            what += f" whose location contains “{a['uri_contains']}”"
        if a.get("id") or a.get("ids"):
            what += " by id"
        return what
    if op == "activities":
        return f"find the runs of {a.get('name') or 'every activity'}"
    if op == "lineage":
        d = {"back": "back (what it came from)", "forward": "forward (what it affected)"}.get(a.get("direction"), a.get("direction") or "")
        return f"follow the lineage of {frm or 'the found entities'} {d}".strip()
    if op == "group":
        return f"group {frm or 'the result'} by {a.get('by', '?')}"
    if op == "ends":
        return f"take the {'roots' if a.get('which', 'roots') == 'roots' else 'leaves'} of {frm or 'the lineage'}"
    if op in ("inputs", "outputs"):
        return f"read the {'inputs (parameters)' if op == 'inputs' else 'outputs'} of every {a.get('activity', '?')} run"
    if op == "attributes":
        return f"read the attributes of {a.get('type') or frm or 'the entities'}"
    return X.OPWORD.get(op, op)


def _grouped(steps: list, words: list) -> list:
    """Consecutive steps of the same kind on different activities in one phrase: "read the inputs (parameters) of every a, b and c run"."""
    out, i = [], 0
    while i < len(steps):
        j = i
        op = steps[i].get("op")
        while j + 1 < len(steps) and op in ("inputs", "outputs", "activities") and steps[j + 1].get("op") == op:
            j += 1
        if j > i:
            names = [str((s.get("args") or {}).get("activity") or (s.get("args") or {}).get("name") or "?") for s in steps[i:j + 1]]
            joined = ", ".join(names[:-1]) + " and " + names[-1]
            out.append({"inputs": f"read the inputs (parameters) of every {joined} run", "outputs": f"read the outputs of every {joined} run",
                        "activities": f"find the runs of {joined}"}[op])
        else:
            out.append(words[i])
        i = j + 1
    return out


def explain(answer: dict) -> dict:
    steps = answer.get("steps") or []
    qs = {q.get("n"): q for q in answer.get("queries") or []}
    who = "you" if (answer.get("plan") or {}).get("chosen_by") == "you" else "the agent"
    words = [_what(s) for s in steps]
    if not steps:
        return X.part("plan", "Plan", "This answer has no plan: it was not computed from the traces.")
    summary = (f"One step: {words[0]}." if len(steps) == 1 else f"{len(steps)} steps: " + "; then ".join(_grouped(steps, words)) + ".") + f" Planned by {who}."
    if len(summary) > X.SUMMARY_MAX:
        g = _grouped(steps, words)
        summary = f"{len(steps)} steps: " + "; then ".join(g[:2]) + f"; and {len(g) - 2} more (see the steps). Planned by {who}."
    details = []
    for s, w in zip(steps, words):
        lead = re.sub(r"\b(\d+) (\w+?)\(s\)", lambda m: f"{m.group(1)} {m.group(2) if m.group(1) == '1' else m.group(2) + 's'}",
                      re.split(r";\s|:\s", s.get("headline") or "", maxsplit=1)[0])
        blocks = [X.block("text", text=f"Found: {X.clip(lead, 160)} (its full result is under Result).")] if lead else []
        args = [[k, v if not isinstance(v, list) else [str(x) for x in v]] for k, v in (s.get("args") or {}).items()]
        res = s.get("resolved") or {}
        tm = res.get("time")
        if isinstance(tm, list) and any(tm):
            args.append(["time window", f"{tm[0] or 'start'} → {tm[1] or 'end'}"])
        if args:
            blocks.append(X.block("kv", rows=args))
        for n in s.get("queries") or []:
            q = qs.get(n) or {}
            blocks.append(X.block("query", label=f"Q{n}", why=q.get("why", ""), code=q.get("query", ""),
                                  meta=f"{X.plural(int(q.get('rows') or 0), 'row')} · {q.get('secs', '?')} s" if q else ""))
        if not s.get("queries") and s.get("computed_from") is not None:
            blocks.append(X.block("note", tone="ok", text=f"computed from S{int(s['computed_from']) + 1}, no new query"))
        blocks.append(X.block("stats", items=[{"label": "records cited", "value": X.fmt_num(int(s.get("cited") or 0))}]))
        details.append({"title": f"S{s.get('step')} · {w}", "pick": f"s{s.get('step')}", "blocks": blocks})
    facts = [{"label": "steps", "value": len(steps)}, {"label": "TraceViewer queries", "value": len(answer.get("queries") or [])}]
    return X.part("plan", "Plan", summary, facts, details)


def register(mcp):
    @mcp.tool()
    def explain_plan(answer: dict) -> dict:
        """Explain the plan of a computed answer (a record from evidence_answer / answers.jsonl): a one-sentence summary of what was asked of
        the traces, and per step its arguments, resolved time window, TraceViewer queries and cited records as typed blocks (core.explain).
        Returns {part: plan, title, summary, facts, details: [{title, pick, blocks}]}."""
        return explain(answer)

    @mcp.tool()
    def explain_plan_check(answer: dict) -> dict:
        """Validator: the plan explanation of this answer is well formed - a short plain summary, numbers that come from the answer, known
        block types, one detail group per step. Returns {status: pass|fail, problems}."""
        p = explain(answer)
        bad = X.check(p, answer, "plan")
        if len(p["details"]) != len(answer.get("steps") or []):
            bad.append("details: one group per step")
        return {"status": "fail" if bad else "pass", "problems": bad}
