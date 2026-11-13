"""Explain the answer of a computed answer: the agent's write-up (when there is one) led by its first sentence, with the check that
every claim cites a step and every number is in the result; without a write-up, the result is the answer. Gaps are named here too."""
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


def explain(answer: dict) -> dict:
    syn = answer.get("synthesis") or ""
    sc = answer.get("synthesis_check") or {}
    gaps = answer.get("gaps") or []
    details = []
    if syn.strip():
        summary = _first_sentence(syn)
        details.append({"title": "The write-up", "blocks": _blocks(syn)})
        details.append({"title": "Checked against the result", "blocks": [X.block("note", tone="ok" if sc.get("ok") else "warn",
                        text="every claim cites a step, and every number is in the computed result" if sc.get("ok") else
                        "; ".join(sc.get("problems") or ["not checked"]))]})
    else:
        lead = re.split(r";\s|:\s", (answer.get("result") or {}).get("headline") or "", maxsplit=1)[0]
        lead = re.sub(r"\b(\d+) (\w+?)\(s\)", lambda m: f"{m.group(1)} {m.group(2) if m.group(1) == '1' else m.group(2) + 's'}", lead)
        summary = "No write-up: the computed result is the answer" + (f" ({X.clip(lead, 140)})." if lead else ".")
    if gaps:
        kinds = sorted({GAP_WORD.get(g.get("kind"), g.get("kind", "?")) for g in gaps})
        summary = X.clip(summary + f" The provenance could not show everything: {X.plural(len(gaps), 'gap')} ({kinds[0]}{', …' if len(kinds) > 1 else ''}).",
                         X.SUMMARY_MAX)
        details.append({"title": "Gaps", "blocks": [X.block("table", columns=["kind", "subject", "step", "detail"], numeric=[2], total=len(gaps),
                                                            rows=[[GAP_WORD.get(g.get("kind"), g.get("kind", "")), g.get("subject", ""), f"S{g.get('step', '?')}",
                                                                   g.get("detail", "")] for g in gaps])]})
    facts = [{"label": "gaps", "value": len(gaps)}] if gaps else []
    return X.part("answer", "Answer", summary, facts, details)


def register(mcp):
    @mcp.tool()
    def explain_answer(answer: dict) -> dict:
        """Explain the answer part of a computed answer: its write-up led by the first sentence (tables and bullets as blocks), the synthesis
        check, and the gaps; without a write-up, says the computed result is the answer. Returns {part: answer, title, summary, facts, details}."""
        return explain(answer)

    @mcp.tool()
    def explain_answer_check(answer: dict) -> dict:
        """Validator: the answer explanation is well formed, its summary's numbers come from the answer, and a write-up's tables are table
        blocks. Returns {status: pass|fail, problems}."""
        p = explain(answer)
        bad = X.check(p, answer, "answer")
        return {"status": "fail" if bad else "pass", "problems": bad}
