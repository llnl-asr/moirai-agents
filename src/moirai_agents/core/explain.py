"""Query explanations: a computed answer (core.evidence) told in four parts - plan, result, answer, evidence - each first as a short
high-level description, then its details as typed blocks the Query view renders one way everywhere. No model: every word and number
comes from the answer record.

The four parts are capabilities of their own (explain_plan, explain_result, explain_answer, explain_evidence), so each can be refined and
learned separately; query_explain runs them. This module is what they share: the block vocabulary, value formatting, and the checks.

A part: {"part", "title", "summary": one or two plain sentences, "facts": [{"label", "value"}] (at most 4 key numbers),
         "details": [{"title", "pick"?: graph selection key, "blocks": [block]}]}
Blocks:
  text    {"text"}                               prose; [S1] cites a step, `code` is code
  kv      {"rows": [[key, value | [values]]]}    name -> value(s); a list shows as chips
  table   {"columns", "rows", "numeric": [i], "total"}   rows as given; numeric columns right-aligned; total when rows were cut
  query   {"label", "why", "code", "meta"}       a TraceViewer query as run
  stats   {"items": [{"label", "value"}]}
  list    {"items": [text]}
  note    {"tone": ok|warn, "text"}
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

BLOCKS = {"text": ("text",), "kv": ("rows",), "table": ("columns", "rows"), "query": ("code",), "stats": ("items",), "list": ("items",),
          "note": ("text",)}
PARTS = ("plan", "result", "answer", "evidence")
SUMMARY_MAX = 320
ROWS_MAX = 200

OPWORD = {"entities": "find the entities", "activities": "find the runs of an activity", "lineage": "follow the lineage", "group": "group",
          "ends": "take the ends of the lineage", "inputs": "read the inputs (parameters) of", "outputs": "read the outputs of",
          "attributes": "read the attributes of"}

from .aspects import opword as _aspect_words  # noqa: E402
OPWORD.update(_aspect_words())


# ------------------------------------------------------------------------------------------------ values

def is_epoch_s(x: float) -> bool:
    return 1.0e9 <= x <= 2.2e9


def fmt_num(x: Any) -> str:
    """A number for people: thousands separators, at most 4 significant decimals, epochs (s or us) as UTC time."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    if is_epoch_s(x):
        ms = f".{int(round((x % 1) * 1000)) % 1000:03d}" if isinstance(x, float) and x % 1 else ""     # runs a moment apart stay apart
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x)) + ms + " UTC"
    if 1.0e15 <= x <= 2.2e15:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x / 1e6)) + f".{int(x % 1e6):06d} UTC"
    if isinstance(x, int) or float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.4g}" if abs(x) >= 1e-3 else f"{x:.3g}"


_NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def fmt_value(v: Any) -> str:
    """A recorded value as text, kept exact (a parameter is shown as it was passed): only epoch times become UTC times and floats with many
    decimals are shortened."""
    s = str(v).strip()
    if _NUM.match(s):
        f = float(s)
        if is_epoch_s(f) or 1.0e15 <= f <= 2.2e15:
            return fmt_num(f)
        if "." in s and len(s.split(".")[1]) > 6:
            return f"{f:.6g}"
    return s


def split_values(v: str) -> List[str]:
    """'a | b | c ...' or 'a,b,c' as separate values (a trailing '...' kept as its own item); one value stays one."""
    s = str(v).strip()
    if " | " in s:
        parts = [p.strip() for p in s.split(" | ")]
    elif "," in s and " " not in s and len(s) < 200:
        parts = [p.strip() for p in s.split(",")]
    else:
        return [s]
    out = []
    for p in parts:
        if p.endswith("...") and p != "...":
            out += [p[:-3].strip(), "…"]
        else:
            out.append("…" if p == "..." else p)
    return [x for x in out if x != ""]


def md_table(md: str) -> Optional[Dict[str, Any]]:
    """A markdown table (as core.evidence writes them) as a table block; None when there is none."""
    lines = [ln.strip() for ln in (md or "").splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return None
    def cells(ln):                     # split on unescaped pipes: a cell may hold "a \\| b" (several values)
        body = ln.strip()[1:-1] if ln.strip().endswith("|") and not ln.strip().endswith("\\|") else ln.strip()[1:]
        return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]
    cols = cells(lines[0])
    rows = [cells(ln) for ln in lines[1:] if not re.match(r"^\|[\s:|-]+\|$", ln)]
    vi = next((i for i, c in enumerate(cols) if c.lower() in ("value(s)", "values", "value")), min(1, len(cols) - 1))
    fixed = []
    for r in rows:                     # unescaped pipes inside a cell (older answers): the extra cells belong to the value column
        if len(r) > len(cols):
            keep = len(cols) - vi - 1
            r = r[:vi] + [" | ".join(r[vi:len(r) - keep])] + (r[len(r) - keep:] if keep else [])
        fixed.append(r + [""] * (len(cols) - len(r)))
    rows = fixed
    extra = [ln for ln in (md or "").splitlines() if ln.strip() and not ln.strip().startswith("|")]
    total = len(rows)
    m = next((re.search(r"(\d[\d,]*) (?:more|rows|in all)", x) for x in extra if re.search(r"(\d[\d,]*) (?:more|rows|in all)", x)), None)
    if m:
        total = max(total, int(m.group(1).replace(",", "")) + (len(rows) if "more" in m.group(0) else 0))
    return table(cols, rows, total=total)


def table(columns: List[str], rows: List[List[Any]], total: Optional[int] = None) -> Dict[str, Any]:
    rows = [[fmt_value(c) if not isinstance(c, list) else c for c in r] for r in rows[:ROWS_MAX]]
    numeric = [i for i in range(len(columns)) if rows and all(_NUM.match(str(r[i]).replace(",", "")) for r in rows if i < len(r) and str(r[i]) not in ("", "-"))
               and any(str(r[i]) not in ("", "-") for r in rows)]
    return {"type": "table", "columns": list(columns), "rows": rows, "numeric": numeric, "total": max(total or 0, len(rows))}


def block(kind: str, **kw) -> Dict[str, Any]:
    return {"type": kind, **kw}


def part(name: str, title: str, summary: str, facts: Optional[List[Dict[str, Any]]] = None, details: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"part": name, "title": title, "summary": " ".join(summary.split()), "facts": facts or [], "details": details or []}


def plural(n: int, word: str, many: str = "") -> str:
    return f"{fmt_num(n)} {word if n == 1 else (many or word + 's')}"


def clip(s: str, n: int = 140) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------------------ checks

def _numbers(s: str) -> List[str]:
    return re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", s or "")


def known_numbers(answer: Dict[str, Any]) -> set:
    """Every number an explanation may state: the ones in the answer, and counts of what it holds (list lengths, table rows, distinct values)."""
    out = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(answer, default=str)))

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            out.add(str(len(x)))
            for v in x:
                walk(v)
        elif isinstance(x, str) and x.count("|") > 3:
            t = md_table(x)
            if t:
                out.update({str(len(t["rows"])), str(t["total"])})
                for r in t["rows"]:
                    for c in r:
                        out.add(str(len(split_values(str(c)))))
                        out.update(n.replace(",", "") for n in _numbers(fmt_value(c)))
    walk(answer)
    return {x.replace(",", "") for x in out}


def check(p: Dict[str, Any], answer: Dict[str, Any], name: str = "") -> List[str]:
    """What is wrong with an explanation part: shape, a summary that is short and plain, numbers that come from the answer, known blocks."""
    bad: List[str] = []
    if not isinstance(p, dict):
        return ["the part is not an object"]
    if name and p.get("part") != name:
        bad.append(f"part is {p.get('part')!r}, not {name!r}")
    s = p.get("summary") or ""
    if not s.strip():
        bad.append("summary: say in a sentence what this part is")
    if len(s) > SUMMARY_MAX:
        bad.append(f"summary: {len(s)} characters; keep it under {SUMMARY_MAX}, put the rest in details")
    if "|" in s and "---" in s:
        bad.append("summary: a table belongs in details, as a table block")
    known = known_numbers(answer)
    for n in _numbers(re.sub(r"`[^`]*`|\[[RS]\d+\]", "", s)) + [str(f.get("value")) for f in p.get("facts") or [] if isinstance(f.get("value"), (int, float))]:
        if n.replace(",", "") not in known and not re.fullmatch(r"[0-9]", n):
            bad.append(f"summary/facts: {n} is neither in the answer nor a count of it")
    if len(p.get("facts") or []) > 4:
        bad.append("facts: at most 4 key numbers at the high level")
    for d in p.get("details") or []:
        for b in d.get("blocks") or []:
            need = BLOCKS.get(b.get("type"))
            if need is None:
                bad.append(f"details {d.get('title')!r}: unknown block type {b.get('type')!r} (one of {sorted(BLOCKS)})")
            elif any(k not in b for k in need):
                bad.append(f"details {d.get('title')!r}: a {b['type']} block needs {need}")
            elif b["type"] == "table" and any(len(r) != len(b["columns"]) for r in b["rows"]):
                bad.append(f"details {d.get('title')!r}: a table row has a different number of cells than columns")
            elif b["type"] == "text" and re.search(r"^\|.*\|\s*$", b["text"], re.M):
                bad.append(f"details {d.get('title')!r}: a markdown table in a text block: make it a table block")
    return bad


# ------------------------------------------------------------------------------------------------ checked against the traces

_FRESH: Dict[Any, Any] = {}
FRESH_S = 120


def load_answer(pipeline: str, answer_id: str) -> Dict[str, Any]:
    from . import answers as A
    rec = next((a for a in A.listing(pipeline, limit=10 ** 6) if a.get("id") == answer_id), None)
    if not rec:
        raise ValueError(f"no answer {answer_id} on {pipeline}")
    return rec


def source_of(pipeline: str, answer: Dict[str, Any]) -> Dict[str, Any]:
    """Which traces to check an answer against: the files it was computed on, when they all still exist (an answer about an earlier
    baseline is checked against that baseline, not the newest one); otherwise the pipeline's traces now. {traces, own, current, newer}."""
    import os
    from . import answers as A, pipeline as P
    current = A.traces_of(P.load(pipeline)) or ""
    paths = [r.get("path") for r in answer.get("reads") or [] if r.get("path")]
    own = bool(paths) and all(os.path.exists(x) for x in paths)
    now = set(provtrace_files(current))
    newer = bool(paths) and bool(now) and not set(paths) <= now
    return {"traces": os.pathsep.join(paths) if own else current, "own": own, "current": current, "newer": newer}


def provtrace_files(traces: str) -> List[str]:
    from . import provtrace as T
    return T.expand(traces) if traces else []


def fresh(pipeline: str, answer: Dict[str, Any]) -> Dict[str, Any]:
    """The answer's plan run again with TraceViewer on the traces it was computed on (kept for FRESH_S seconds, so the four parts'
    validators share one run): {result_hash, steps, result, reads, queries, stats, source}."""
    from . import evidence as EV
    src = source_of(pipeline, answer)
    if not src["traces"]:
        raise ValueError(f"{pipeline} has no traces to check against")
    key = (pipeline, answer.get("id"), answer.get("result_hash"), src["traces"])
    hit = _FRESH.get(key)
    if hit and time.time() - hit[0] < FRESH_S:
        return hit[1]
    r = EV.run(src["traces"], (answer.get("plan") or {}).get("steps") or [], EV.index_dir_of(pipeline), pipeline, "explain")
    last = r["steps"][-1] if r["steps"] else {}
    out = {"result_hash": r["result_hash"], "steps": r["steps"], "result": {"headline": last.get("headline", ""), "table": last.get("table", "")},
           "reads": r["reads"], "queries": r["queries"], "stats": r["stats"], "source": src}
    _FRESH[key] = (time.time(), out)
    return out


def against(pipeline: str, answer: Optional[Dict[str, Any]], answer_id: str, part_check) -> Dict[str, Any]:
    """Run a part's validator: the explanation's shape and numbers against the answer record, and - with a pipeline - every claim against the
    traces the answer was computed on (its plan run again). part_check(answer, fresh_or_None) -> problems. When the traces no longer give the
    answer's result, that one finding is reported, not every row it touches. Returns {status, problems, data, note}."""
    a = answer if answer else load_answer(pipeline, answer_id)
    if not pipeline:
        bad = part_check(a, None)
        return {"status": "fail" if bad else "pass", "problems": bad, "data": "not checked against the traces: give pipeline (and answer_id)", "note": ""}
    try:
        f = fresh(pipeline, a)
    except Exception as e:                                # noqa: BLE001 - a check that cannot run is a failed check, said why
        return {"status": "fail", "problems": [f"could not check against the traces: {e}"], "data": "not checked", "note": ""}
    src = f["source"]
    files = ", ".join(sorted({r.get("file", "") for r in f["reads"]}))
    mine = ", ".join(sorted({r.get("file", "") for r in a.get("reads") or []}))
    note = (f"Computed on {mine}, an earlier run than the app's traces now: ask again to answer about the current run."
            if src["newer"] and src["own"] else "")
    reordered = f["result_hash"] != a.get("result_hash") and same_result(a, f)
    if reordered:                                         # the same records and values, listed in another order: the record stands
        note = (note + " " if note else "") + "The traces give the same records and values, listed in another order."
        bad = part_check(a, None)
    elif f["result_hash"] != a.get("result_hash"):
        bad = part_check(a, None)                         # the record itself still has to hold; row-by-row against changed traces says nothing new
        gone = missing_files(a)
        bad.insert(0, (f"the traces it was computed on ({', '.join(gone)}) are gone; the app's traces now ({files}) give another result. Ask the question again."
                       if gone else f"the traces ({files}) no longer give this result: {a.get('result_hash')} then, {f['result_hash']} now. Ask the question again."))
    else:
        bad = part_check(a, f)
    data = (f"checked against {'the traces it was computed on' if src['own'] else 'the app traces now'} ({files}): the plan ran again, "
            f"{plural(len(f['queries']), 'TraceViewer query', 'TraceViewer queries')}, {plural(int(f['stats'].get('rows_read') or 0), 'record')} read")
    return {"status": "fail" if bad else "pass", "problems": bad, "data": data, "note": note}


def missing_files(answer: Dict[str, Any]) -> List[str]:
    import os
    return [r.get("file", "") for r in answer.get("reads") or [] if r.get("path") and not os.path.exists(r["path"])]


def _tokens(s: str) -> List[str]:
    return sorted(t for t in re.split(r"[\s|;,=]+", (s or "").replace("\\|", "|")) if t and t not in ("…", "...", "---"))


def same_result(answer: Dict[str, Any], f: Dict[str, Any]) -> bool:
    """The traces give the same result as the answer, up to order: per step the same values (as a multiset of tokens), or - when a long
    list was cut to its first values - the same records cited and the same counts."""
    old, new = answer.get("steps") or [], f.get("steps") or []
    if len(old) != len(new):
        return False
    key = lambda c: (c.get("file"), c.get("pid"), c.get("ts"), c.get("id"))       # noqa: E731
    for o, n in zip(old, new):
        if _tokens(o.get("headline", "") + " " + o.get("table", "")) == _tokens(n.get("headline", "") + " " + n.get("table", "")):
            continue
        if (int(o.get("cited") or 0) == int(n.get("cited") or 0) and int(o.get("cited") or 0) <= len(o.get("cites") or [])
                and {key(c) for c in o.get("cites") or []} == {key(c) for c in n.get("cites") or []}
                and re.split(r";\s|:\s", o.get("headline", ""), maxsplit=1)[0] == re.split(r";\s|:\s", n.get("headline", ""), maxsplit=1)[0]):
            continue
        return False
    return True


def cell_text(c: Any) -> str:
    return " | ".join(map(str, c)) if isinstance(c, list) else str(c)


def claims(text: str) -> List[Any]:
    """The claims of a narrative: (quoted values, [R n] rows, [S n] steps) per clause - a clause ends at a link or at ; . : boundaries."""
    out = []
    for clause in re.split(r"(?<=\])[;,.]?\s+|;\s+", text or ""):
        rows = [int(n) for n in re.findall(r"\[R(\d+)\]", clause)]
        steps = [int(n) for n in re.findall(r"\[S(\d+)\]", clause)]
        vals = [(v[2:] if v.startswith("…/") else v).rstrip("…") for v in re.findall(r"`([^`]*)`", clause)]
        if (rows or steps) and vals:
            out.append((vals, rows, steps))
    return out
