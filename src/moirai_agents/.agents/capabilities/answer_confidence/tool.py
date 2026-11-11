"""Confidence of an answer: how much of it was calculated - TraceViewer queries on the traces - and how much was inferred, by the LLM or not
at all (a gap). Every piece of the answer is an item with how it was obtained and a weight; the score is their weighted share, 0-100.
Anything the LLM inferred instead of calculating is flagged. Deterministic: the score is computed from the answer record and, with a
pipeline, from the plan run again on the traces."""
import re

from moirai_agents.core import explain as X

HOW = {   # how a piece of the answer was obtained -> (weight, words)
    "query": (1.0, "calculated: a TraceViewer query on the traces"),
    "derived": (0.9, "calculated from an earlier step's result, no new query"),
    "narrative": (1.0, "written from the computed result, every value linked and checked"),
    "llm_verified": (1.0, "values from a query, worded by the LLM: every value validated against the query results"),
    "wording": (None, "the LLM's wording, no value in it: not counted"),
    "llm_query": (0.3, "a query the agent ran itself, summarized by the LLM (not reproducible here)"),
    "llm": (0.0, "a value the LLM derived: no query computed it"),
    "gap": (0.0, "not shown by the provenance: a gap"),
}
LABELS = ((90, "calculated"), (70, "mostly calculated"), (40, "partly inferred"), (0, "mostly inferred"))
STALE = 0.5


def _sentences(text: str):
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or re.match(r"^\|[\s:|-]+\|$", s):
            continue
        if s.startswith("|"):
            out.append(s)                                 # a table row is one claim
            continue
        s = re.sub(r"^([-*]|\d+\.)\s+", "", s)
        out += [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z`\[(])", s) if x.strip()]
    return out


def _values(sent: str) -> list:
    """The values a sentence states: quoted ones and numbers."""
    q = [v for v in re.findall(r"`([^`]*)`", sent) if v and not v.startswith("--") and " " not in v]
    return q + _numbers(re.sub(r"`[^`]*`", "", sent))


def _computed_text(answer: dict) -> str:
    return " ".join((s.get("headline") or "") + " " + (s.get("table") or "") for s in answer.get("steps") or []).replace("\\|", "|")


def _numbers(s: str):
    return [n.replace(",", "") for n in re.findall(r"(?<![\w.\[:-])\d[\d,]*(?:\.\d+)?(?![\w\]])", re.sub(r"\[[SR]\d+\]", "", s))]


def items(answer: dict) -> list:
    """Every piece of the answer, with how it was obtained (a key of HOW)."""
    out = []
    if answer.get("engine") == "traceviewer":
        for s in answer.get("steps") or []:
            how = "query" if s.get("queries") else "derived"
            out.append({"what": f"S{s.get('step')}: {X.clip(s.get('headline') or s.get('op', ''), 120)}", "how": how, "ref": f"S{s.get('step')}"})
        out.append({"what": "the answer narrative (from the result table and steps)", "how": "narrative", "ref": "answer"})
        computed = _computed_text(answer)
        known = X.known_numbers(answer)
        results = X.known_numbers({"steps": [{"headline": s.get("headline", ""), "table": s.get("table", "")} for s in answer.get("steps") or []]})  # query outputs and their counts
        for sent in _sentences(answer.get("synthesis") or ""):
            cites = re.findall(r"\[S(\d+)\]", sent)
            vals = _values(sent)
            where = " ".join((s.get("headline") or "") + " " + (s.get("table") or "") for s in answer.get("steps") or []
                             if not cites or str(s.get("step")) in cites).replace("\\|", "|")
            nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", where)} | results     # whole numbers only: 40 is not in 575400
            unfound = [v for v in vals if (v.replace(",", "") not in nums if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", v) else v not in where)]
            how = "wording" if not vals else "llm" if unfound else "llm_verified"
            item = {"what": X.clip(sent, 160), "how": how, "ref": "write-up", "llm": True}
            if unfound:
                item["why"] = f"{', '.join(unfound[:4])} {'is' if len(unfound) == 1 else 'are'} not in any query result" + (" of the cited step" if cites else "")
            out.append(item)
    else:                                                 # an agent-written answer: its evidence was gathered and worded by the LLM
        for e in answer.get("evidence") or []:
            k = e.get("kind", "")
            how = "llm_query" if k in ("query", "count", "lineage") else "llm"
            out.append({"what": X.clip(e.get("text", ""), 160), "how": how, "ref": "evidence", "llm": True,
                        "why": "the agent ran it and wrote it up; it was not recomputed" if how == "llm_query" else f"{k or 'evidence'} read or reasoned by the LLM"})
        if answer.get("answer"):
            out.append({"what": X.clip(answer.get("answer"), 160), "how": "llm", "ref": "answer", "llm": True, "why": "the answer was written by the LLM"})
    for g in answer.get("gaps") or []:
        if isinstance(g, dict):
            out.append({"what": f"{g.get('kind')}: {g.get('subject')}", "how": "gap", "ref": f"S{g.get('step', '?')}"})
        else:
            out.append({"what": X.clip(str(g), 160), "how": "gap", "ref": "gap"})
    for it in out:
        it["weight"], it["meaning"] = HOW[it["how"]]
    return out


def score_of(its: list, stale: bool) -> int:
    its = [i for i in its if i["weight"] is not None]        # wording without values counts neither way
    if not its:
        return 0
    s = sum(i["weight"] for i in its) / len(its)
    return int(round(100 * s * (STALE if stale else 1.0)))


def confidence(answer: dict, pipeline: str = "") -> dict:
    its = items(answer)
    stale, data = None, "not checked against the traces (no pipeline)"
    if pipeline and answer.get("engine") == "traceviewer":
        try:
            f = X.fresh(pipeline, answer)
            stale = f["result_hash"] != answer.get("result_hash") and not X.same_result(answer, f)
            data = ("the traces it was computed on no longer give this result: ask again" if stale else
                    "the plan ran again on the traces it was computed on: same result" +
                    ("; newer traces exist for this app" if f["source"]["newer"] else ""))
        except Exception as e:                            # noqa: BLE001
            data = f"could not check against the traces: {e}"
    sc = score_of(its, bool(stale))
    by = {}
    for i in its:
        by[i["how"]] = by.get(i["how"], 0) + 1
    calc = sum(1 for i in its if i["how"] in ("query", "derived", "narrative", "llm_verified"))
    counted = sum(1 for i in its if i["weight"] is not None)
    inferred = [i for i in its if i["how"] in ("llm", "llm_query")]
    label = next(l for t, l in LABELS if sc >= t)
    words = (f"{sc}/100, {label}: {calc} of {counted} parts with values come from queries on the traces" +
             (f" ({by['llm_verified']} of them worded by the LLM, every value validated)" if by.get("llm_verified") else "") +
             (f"; {len(inferred)} with values the LLM derived" if inferred else "") +
             (f", {by['gap']} not shown by the provenance" if by.get("gap") else "") + "." +
             (" Halved: the traces no longer give this result." if stale else ""))
    return {"score": sc, "label": label, "summary": words, "counts": by, "calculated": calc, "total": counted, "llm_inferred": len(inferred),
            "stale": stale, "data": data, "items": its, "weights": {k: v[0] for k, v in HOW.items()}}


def register(mcp):
    @mcp.tool()
    def confidence_score(pipeline: str = "", answer_id: str = "", answer: dict = None) -> dict:
        """The confidence of an answer, 0-100: the weighted share of its parts with values whose values were calculated by TraceViewer queries
        on the traces (query 1.0, derived 0.9, narrative 1.0, LLM wording whose every value is validated against the query results 1.0) rather
        than derived by the LLM (a value no query computed 0, an agent's own unrecomputed query 0.3) or missing (a gap 0); wording without
        values is not counted; halved when the traces no longer give the result. Every value the LLM derived is flagged with the value. Give pipeline and answer_id (or the answer). Returns {score, label, summary, counts, items, ...}."""
        return confidence(answer if answer else X.load_answer(pipeline, answer_id), pipeline)

    @mcp.tool()
    def confidence_validate(pipeline: str = "", answer_id: str = "", answer: dict = None) -> dict:
        """Validator: the confidence is computed again independently and must agree - every step, write-up sentence, evidence item and gap is
        an item; every LLM-written item is flagged; the score is the weighted share (halved when stale); and, with a pipeline, whether the
        traces still give the result is checked by running the plan again. Returns {status: pass|fail, problems, data}."""
        a = answer if answer else X.load_answer(pipeline, answer_id)
        c = confidence(a, pipeline)
        bad = []
        if a.get("engine") == "traceviewer":
            n_steps, n_sent = len(a.get("steps") or []), len(_sentences(a.get("synthesis") or ""))
            if sum(1 for i in c["items"] if i["how"] in ("query", "derived")) != n_steps:
                bad.append("every step must be an item")
            if sum(1 for i in c["items"] if i["ref"] == "write-up") != n_sent:
                bad.append("every sentence of the write-up must be an item")
            for s in a.get("steps") or []:
                it = next((i for i in c["items"] if i["ref"] == f"S{s.get('step')}" and i["how"] in ("query", "derived")), None)
                if it and it["how"] == "query" and not s.get("queries"):
                    bad.append(f"S{s.get('step')} ran no query: it cannot count as calculated by a query")
        else:
            if sum(1 for i in c["items"] if i["ref"] == "evidence") != len(a.get("evidence") or []):
                bad.append("every evidence item must be an item")
            if any(i["how"] in ("query", "derived", "narrative") for i in c["items"]):
                bad.append("an agent-written answer has nothing calculated here: its evidence was not recomputed")
        bad += [f"{i['what']!r} was written by the LLM but is not flagged" for i in c["items"] if i["how"].startswith("llm") and not i.get("llm")]
        if sum(1 for i in c["items"] if i["how"] == "gap") != len(a.get("gaps") or []):
            bad.append("every gap must be an item")
        if c["score"] != score_of(c["items"], bool(c["stale"])) or not 0 <= c["score"] <= 100:
            bad.append("the score is not the weighted share of the items")
        if c["stale"] is None and pipeline and a.get("engine") == "traceviewer":
            bad.append(f"could not be checked against the traces: {c['data']}")
        return {"status": "fail" if bad else "pass", "problems": bad, "data": c["data"]}
