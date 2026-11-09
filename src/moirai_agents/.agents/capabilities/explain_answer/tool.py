"""Explain the answer of a computed answer: always a narrative that answers the question from the computed result - the direct answer
first, then what differs, what stays the same and how it was found - every claim linked to where it can be checked: [R3] is row 3 of the
result table, [S2] is step 2 of the plan. Written from the record, no model. The agent's write-up (when there is one) and the gaps follow."""
import re

from moirai_agents.core import explain as X

GAP_WORD = {"undeclared_entity": "an entity used but never declared", "missing_producer": "an entity nothing recorded producing",
            "no_inputs": "an activity without its inputs", "no_outputs": "an activity without its outputs",
            "no_attributes": "entities without attributes", "not_recorded": "something the question names that is not recorded"}


def _first_sentence(text: str) -> str:
    """The write-up's opening: its first line, up to the end of its first sentence; a line that leads into a list ends with an ellipsis."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip() and not l.strip().startswith(("|", "#", "-", "*"))), "")
    line = re.sub(r"[*_`>]+", "", line).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", line)
    first = m.group(1) if m else line
    if first.endswith(":"):
        first = first[:-1].rstrip() + "…"
    return X.clip(first, 260)


def _blocks(md: str) -> list:
    """A markdown write-up as blocks: tables as table blocks, bullet runs as lists, paragraphs as text."""
    out, buf, items, tab = [], [], [], []

    def text():
        if buf:
            out.append(X.block("text", text="\n".join(buf)))
            buf.clear()

    def bullets():
        if items:
            out.append(X.block("list", items=list(items)))
            items.clear()

    def rows():
        if tab:
            out.append(X.md_table("\n".join(tab)) or X.block("text", text="\n".join(tab)))
            tab.clear()
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("|"):
            text()
            bullets()
            tab.append(s)
            continue
        rows()
        if re.match(r"^[-*] ", s):
            text()
            items.append(s[2:])
        elif s:
            bullets()
            buf.append(s)
        else:
            text()
            bullets()
    text()
    bullets()
    rows()
    return out


LEAD = re.compile(r"\b(\d+) (\w+?)\(s\)")
NOUN = {"inputs": "parameter", "outputs": "output", "attributes": "attribute"}
VALUE_COLS = ("value(s)", "values", "value")


def _clean(s: str) -> str:
    return LEAD.sub(lambda m: f"{m.group(1)} {m.group(2) if m.group(1) == '1' else m.group(2) + 's'}", s or "").replace("(s)", "s")


def _split(headline: str):
    """(what was found, what follows): the headline's first clause and the rest."""
    parts = re.split(r";\s|:\s", _clean(headline or ""), maxsplit=1)
    return parts[0].strip().rstrip("."), (parts[1].strip().rstrip(".") if len(parts) > 1 else "")


def _num(v: str):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _code(v: str, n: int = 60) -> str:
    v = X.fmt_value(str(v))
    if v.startswith("/") and len(v) > n:
        v = "…/" + "/".join(v.rstrip("/").rsplit("/", 2)[-2:])          # the file and its folder: names repeat across folders
    return "`" + X.clip(v, n).replace("`", "'") + "`" if v != "" else "empty"


def _values(cell, k: int) -> str:
    vals = [v for v in X.split_values(cell) if v != "…"]
    more = "…" in X.split_values(cell)
    nums = [_num(v) for v in vals]
    if vals and all(x is not None for x in nums) and not more:
        lo, hi = vals[nums.index(min(nums))], vals[nums.index(max(nums))]
        return f"{len(vals)} values, from {_code(lo)} to {_code(hi)}"
    shown = ", ".join(_code(v, 40) for v in vals[:3])
    return f"{len(vals)}{' or more' if more else ''} values, such as {shown}"


def _value_table(t, vc, noun, last, lead):
    """The narrative of a name -> value(s) table (parameters, outputs, attributes): what differs, what is the same, each with its row."""
    per = {"runs": "run", "entities": "entity"}.get((t["columns"][2] if len(t["columns"]) > 2 else "").lower(), "run")
    pers = "entities" if per == "entity" else per + "s"
    rows = list(enumerate(t["rows"], 1))
    varied = [(i, r) for i, r in rows if len([v for v in X.split_values(r[vc]) if v != "…"]) > 1 or "…" in X.split_values(r[vc])]
    same = [(i, r) for i, r in rows if (i, r) not in varied]
    nouns = noun + ("" if t["total"] == 1 else "s")
    paras = [f"{lead}, with {t['total']} recorded {nouns} [S{last}]."]
    if varied:
        each = "; ".join(f"{_code(r[0])} takes {_values(r[vc], 0)} [R{i}]" for i, r in varied[:4])
        more = f"; and {len(varied) - 4} more" if len(varied) > 4 else ""
        paras.append(f"{len(varied)} of them {'differs' if len(varied) == 1 else 'differ'} between {pers}: {each}{more}.")
    if same:
        flags = [x for x in same if str(x[1][0]).startswith("-") or "." not in str(x[1][0])] or same
        eg = ", ".join(f"{_code(r[0])} = {_code(r[vc], 40)} [R{i}]" for i, r in flags[:4])
        who = ("The other one is" if len(same) == 1 else f"The other {len(same)} are") if varied else ("It is" if len(same) == 1 else f"All {len(same)} are")
        paras.append(f"{who} the same in every {per}, for example {eg}.")
    return paras


def _step_line(s: dict) -> str:
    """One step in the narrative: what it found and, for a table of values, how many and which differ, linked to the step."""
    n, lead = s.get("step"), _split(s.get("headline") or "")[0] or s.get("op", "")
    t = X.md_table(s.get("table") or "")
    vc = next((i for i, c in enumerate([c.lower() for c in t["columns"]]) if c in VALUE_COLS), None) if t else None
    if vc is None or not t["rows"]:
        return f"{lead} [S{n}]"
    noun = NOUN.get(s.get("op"), "value")
    varied = [r for r in t["rows"] if len([v for v in X.split_values(r[vc]) if v != "…"]) > 1 or "…" in X.split_values(r[vc])]
    names = ", ".join(_code(r[0]) for r in varied[:3]) + (" …" if len(varied) > 3 else "")
    return (f"{lead}: {t['total']} {noun}{'' if t['total'] == 1 else 's'}" + (f", {len(varied)} differ ({names})" if varied else ", none differ") + f" [S{n}]")


def narrative(answer: dict):
    """The answer told from the computed result, as paragraphs with [R] / [S] links, and the steps that led there."""
    steps = answer.get("steps") or []
    last = steps[-1].get("step", len(steps)) if steps else 1
    res = answer.get("result") or {}
    lead, rest = _split(res.get("headline") or "")
    t = X.md_table(res.get("table") or "")
    op = steps[-1].get("op", "") if steps else ""
    paras = []
    if t:
        cols = [c.lower() for c in t["columns"]]
        vc = next((i for i, c in enumerate(cols) if c in VALUE_COLS), None)
        cnt = next((i for i, c in enumerate(cols) if c in ("count", "n", "runs") and i > 0), None)
        if vc is not None and t["rows"]:
            paras += _value_table(t, vc, NOUN.get(op, "value"), last, lead)
        elif cnt is not None and t["rows"]:
            top = sorted(enumerate(t["rows"], 1), key=lambda x: -(_num(x[1][cnt]) or 0))[:3]
            src = _split(steps[-2].get("headline") or "")[0] if len(steps) > 1 else ""
            lead = f"{src} grouped {lead[0].lower() + lead[1:]}" if src and len(lead.split()) < 4 else lead
            paras.append(f"{lead} [S{last}]. The largest: " + ", ".join(f"{_code(r[0])} with {r[cnt]} [R{i}]" for i, r in top) +
                         (f", out of {t['total']} groups." if t["total"] > 3 else "."))
        else:
            name = next((i for i, c in enumerate(cols) if c in ("entity", "uri", "name", "activity")), 0)
            shown = ", ".join(f"{_code(r[name])} [R{i}]" for i, r in enumerate(t["rows"][:3], 1))
            paras.append(f"{lead} [S{last}]: {shown}" + (f" and {t['total'] - 3} more (all in the result table)." if t["total"] > 3 else "."))
    else:
        paras.append(f"{lead or 'Nothing was found'} [S{last}]" + (f": {rest}." if rest else "."))
    how = [_step_line(s) for s in steps] if len(steps) > 1 else []
    return paras, how


def explain(answer: dict) -> dict:
    syn = answer.get("synthesis") or ""
    sc = answer.get("synthesis_check") or {}
    gaps = answer.get("gaps") or []
    paras, how = narrative(answer)
    summary = paras[0]
    if len(paras) > 1 and len(summary) + len(paras[1]) < X.SUMMARY_MAX:
        summary += " " + paras[1]
    rest = paras[1:] if summary == paras[0] else paras[2:]
    body = [X.block("text", text=p) for p in rest]
    if how:
        body += [X.block("text", text="How it was found:"), X.block("list", items=how)]
    if gaps:
        kinds = sorted({GAP_WORD.get(g.get("kind"), g.get("kind", "?")) for g in gaps})
        body.append(X.block("note", tone="warn", text=f"The provenance could not show everything: {X.plural(len(gaps), 'gap')} ({', '.join(kinds)}), listed below."))
    body.append(X.block("note", tone="ok", text="To check it: [R…] opens that row of the result table, [S…] selects that step and its records on the graph; "
                                                 "every record read and cited is under Evidence."))
    details = []
    if syn.strip():
        details.append({"title": "The agent's write-up", "blocks": _blocks(syn)})
        details.append({"title": "Write-up checked against the result", "blocks": [X.block("note", tone="ok" if sc.get("ok") else "warn",
                        text="every claim cites a step, and every number is in the computed result" if sc.get("ok") else
                        "; ".join(sc.get("problems") or ["not checked"]))]})
    if gaps:
        details.append({"title": "Gaps", "blocks": [X.block("table", columns=["kind", "subject", "step", "detail"], numeric=[], total=len(gaps),
                                                            rows=[[GAP_WORD.get(g.get("kind"), g.get("kind", "")), g.get("subject", ""), f"S{g.get('step', '?')}",
                                                                   g.get("detail", "")] for g in gaps])]})
    p = X.part("answer", "Answer", summary, [{"label": "gaps", "value": len(gaps)}] if gaps else [], details)
    p["body"] = body
    return p


def _links(text: str):
    return [int(n) for n in re.findall(r"\[R(\d+)\]", text)], [int(n) for n in re.findall(r"\[S(\d+)\]", text)]


def register(mcp):
    @mcp.tool()
    def explain_answer(answer: dict) -> dict:
        """Explain the answer part of a computed answer: its write-up led by the first sentence (tables and bullets as blocks), the synthesis
        check, and the gaps; without a write-up, says the computed result is the answer. Returns {part: answer, title, summary, facts, details}."""
        return explain(answer)

    @mcp.tool()
    def explain_answer_check(answer: dict = None, pipeline: str = "", answer_id: str = "") -> dict:
        """Validator: the answer is a narrative that answers from the result and links where each claim can be checked ([R n] a row of the
        result table, [S n] a step), with links that exist and numbers from the answer; against the traces, every value a claim quotes is in
        the row or step it links, as the traces give it now. Give pipeline and answer_id (or answer and pipeline) to check against the traces:
        the plan runs again with TraceViewer. Without a pipeline only the record is checked. Returns {status: pass|fail, problems, data}."""
        def part_check(a, f):
            p = explain(a)
            bad = X.check(p, a, "answer")
            body_blocks = p.get("body") or []
            text = p["summary"] + " " + " ".join(b.get("text", "") + " " + " ".join(b.get("items", [])) for b in body_blocks)
            rows, steps = _links(text)
            if not rows and not steps:
                bad.append("the answer links nothing: cite the result rows [R n] and steps [S n] it rests on")
            if "No write-up" in text:
                bad.append("the answer must answer, not point at the result")
            src = f or a
            t = X.md_table((src.get("result") or {}).get("table") or "")
            n, k = (len(t["rows"]) if t else 0), len(src.get("steps") or [])
            bad += [f"[R{r}] is not a row of the result table ({n} rows)" for r in rows if not 1 <= r <= n]
            bad += [f"[S{x}] is not a step ({k} steps)" for x in steps if not 1 <= x <= max(k, 1)]
            bad += X.check({**p, "summary": "x", "facts": [], "details": [{"title": "body", "blocks": body_blocks}]}, a, "answer")
            known = X.known_numbers(src)
            plain = re.sub(r"\[[RS]\d+\]|`[^`]*`", "", text)
            bad += [f"{m} is neither in the {'traces' if f else 'answer'} nor a count of it" for m in re.findall(r"(?<![\w.:-])\d[\d,]*(?:\.\d+)?", plain)
                    if m.replace(",", "") not in known and not re.fullmatch(r"[0-9]", m)]
            if f:                                         # every quoted value is where its link points, in the traces' own result
                def truth_row(r):
                    row = t["rows"][r - 1] if t and 1 <= r <= len(t["rows"]) else []
                    return " ".join(X.cell_text(c) + " " + " ".join(X.fmt_value(v) for v in X.split_values(X.cell_text(c))) for c in row)

                def truth_step(x):
                    s = f["steps"][x - 1] if 1 <= x <= len(f["steps"]) else {}
                    return (s.get("headline") or "") + " " + (s.get("table") or "").replace("\\|", "|")
                for vals, rws, sts in X.claims(text):
                    hay = " ".join([truth_row(r) for r in rws] + ([truth_step(x) for x in sts] if not rws else []))
                    hay += " " + " ".join(X.fmt_value(w) for w in re.findall(r"[\w.:/+-]+", hay))
                    for v in vals:
                        if v and v not in hay:
                            bad.append(f"{v!r} is not in {', '.join([f'R{r}' for r in rws] or [f'S{x}' for x in sts])} of the traces' result")
            return bad
        return X.against(pipeline, answer, answer_id, part_check)
