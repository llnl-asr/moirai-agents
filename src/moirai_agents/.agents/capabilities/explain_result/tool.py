"""Explain the result of a computed answer: what the traces gave, first in a sentence (how much, and what stands out: values that differ
between runs), then the result as a real table - several values as chips, numbers and times formatted - and each step's own result."""
import re

from moirai_agents.core import explain as X

VALUE_COLS = ("value(s)", "values", "value")


def _clean(s: str) -> str:
    s = re.sub(r"\b(\d+) (\w+?)\(s\)", lambda m: f"{m.group(1)} {m.group(2) if m.group(1) == '1' else m.group(2) + 's'}", s or "")
    return s.replace("(s)", "s")


def _lead(headline: str) -> str:
    """The headline's first clause: what was found, without the list that follows."""
    h = _clean(headline)
    first = re.split(r";\s|:\s", h, maxsplit=1)[0]
    return X.clip(first, 160).rstrip(".")


def _chips(t: dict) -> dict:
    """A value column with several values per cell becomes chips; returns the cells that vary."""
    cols = [c.lower() for c in t["columns"]]
    vc = next((i for i, c in enumerate(cols) if c in VALUE_COLS), None)
    varied = []
    if vc is not None:
        for r in t["rows"]:
            vals = X.split_values(r[vc])
            if len(vals) > 1:
                r[vc] = [X.fmt_value(v) for v in vals]
                varied.append((r[0], len([v for v in vals if v != "…"]), vals[-1] == "…"))
    return {"varied": varied, "value_col": vc}


NOUN = {"inputs": "parameter", "outputs": "output", "attributes": "attribute"}


def explain(answer: dict) -> dict:
    res = answer.get("result") or {}
    steps = answer.get("steps") or []
    head = res.get("headline") or ""
    t = X.md_table(res.get("table") or "")
    details, facts = [], []
    summary = _lead(head) + "." if head else "No result."
    if t:
        info = _chips(t)
        noun = (t["columns"][0] if t["columns"] else "row").lower()
        last = (steps[-1].get("op") if steps else "") or ""
        noun = NOUN.get(last) or {"name": "value", "entity": "entity", "type": "type"}.get(noun, noun)
        summary = f"{_lead(head)}: {X.plural(t['total'], noun, noun if noun.endswith('s') else '')}"
        if info["value_col"] is not None:
            if info["varied"]:
                names = ", ".join(f"{n} ({k}{'+' if more else ''} values)" for n, k, more in info["varied"][:3])
                more = f" and {len(info['varied']) - 3} more" if len(info["varied"]) > 3 else ""
                summary += f"; {len(info['varied'])} differ between runs: {names}{more}"
            else:
                summary += ", the same in every run"
        summary = X.clip(summary + ".", X.SUMMARY_MAX)
        facts.append({"label": noun if t["total"] == 1 or noun.endswith("s") else noun + "s", "value": t["total"]})
        if info["varied"]:
            facts.append({"label": "differ between runs", "value": len(info["varied"])})
        t["anchor"] = "result"                     # its rows are R1, R2, ...: the answer links them
        details.append({"title": "The result", "blocks": [t] + ([X.block("note", tone="warn", text=f"showing {len(t['rows'])} of {X.fmt_num(t['total'])} rows")]
                                                               if t["total"] > len(t["rows"]) else [])})
    if head and not t:
        details.append({"title": "In one line", "blocks": [X.block("text", text=_clean(head))]})
    if len(steps) > 1:
        for s in steps[:-1]:
            st = X.md_table(s.get("table") or "")
            if st:
                _chips(st)
            details.append({"title": f"S{s.get('step')}: its own result", "pick": f"s{s.get('step')}",
                            "blocks": [X.block("text", text=_clean(s.get("headline") or ""))] + ([st] if st else [])})
    return X.part("result", "Result", summary, facts, details)


def register(mcp):
    @mcp.tool()
    def explain_result(answer: dict) -> dict:
        """Explain the result of a computed answer: a summary sentence (how much was found, which values differ between runs), the result as
        a table block (several values as chips, numbers and epoch times formatted), and each earlier step's own result.
        Returns {part: result, title, summary, facts, details}."""
        return explain(answer)

    @mcp.tool()
    def explain_result_check(answer: dict = None, pipeline: str = "", answer_id: str = "") -> dict:
        """Validator: the result explanation is well formed (summary from the answer, the result as a table block) and - against the traces -
        the result table is the one the traces give, and every value the explanation shows is in it.
        Give pipeline and answer_id (or answer and pipeline) to check against the traces: the plan runs again with TraceViewer and every row and value shown is looked up in the result it computes.
        Without a pipeline only the record is checked. Returns {status: pass|fail, problems, data: what was checked against}."""
        def part_check(a, f):
            p = explain(a)
            bad = X.check(p, a, "result")
            if X.md_table((a.get("result") or {}).get("table") or "") and not any(b.get("type") == "table" for d in p["details"] for b in d["blocks"]):
                bad.append("details: the result table must be a table block")
            if f:
                if (f["result"].get("table") or "") != ((a.get("result") or {}).get("table") or ""):
                    bad.append("the result table the traces give now differs from the one explained")
                ft = X.md_table(f["result"].get("table") or "")
                shown = next((b for d in p["details"] if d.get("title") == "The result" for b in d["blocks"] if b.get("type") == "table"), None)
                if shown and ft:
                    for i, row in enumerate(shown["rows"]):
                        truth = " | ".join(X.cell_text(c) for c in (ft["rows"][i] if i < len(ft["rows"]) else []))
                        truth += " " + " ".join(X.fmt_value(v) for c in (ft["rows"][i] if i < len(ft["rows"]) else []) for v in X.split_values(X.cell_text(c)))
                        for c in row:
                            for v in (c if isinstance(c, list) else [c]):
                                if v not in ("…", "", "-") and str(v) not in truth:
                                    bad.append(f"row {i + 1}: {X.clip(v, 60)!r} is not in the traces' result")
                                    break
                if not X.check({**p, "details": []}, f, "result") == []:
                    bad += [x.replace("in the answer", "in the traces' result") for x in X.check({**p, "details": []}, f, "result")]
            return bad
        return X.against(pipeline, answer, answer_id, part_check)
