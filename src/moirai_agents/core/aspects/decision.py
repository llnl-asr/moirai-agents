"""Decision making: why trust the result. Its quality and quantity metrics (the stats operation over recorded attributes and outputs), and
what failed or passed: the status-like values the annotation captured (return codes, exit codes, ok/success/failed flags, errors) counted
by value, with the failures named."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

from . import _common as C

ASPECT, LABEL = "decision", "Decision making"
SCOPE = ("Why trust the result: quality metrics (scores, confidences, error estimates), quantity (how many items were produced, how many "
         "inputs each used), and what failed or passed (return and exit codes, status flags, errors, validation checks), so a result can be "
         "accepted or rejected on evidence.")
CORE_OPS = ("stats",)
CAPTURE = ("quality and quantity metrics as attributes of the entities they describe (e.g. mean_plddt=, n_seqs=), and every step's outcome "
           "as an output or attribute: returncode=, status=, ok=, error= (prov_annotate capture.outputs / attributes).")
STATUS_NAMES = ("returncode", "return_code", "exit_code", "exitcode", "rc", "status", "state", "ok", "success", "succeeded", "failed", "passed",
                "valid", "error", "errors")
FAILED = re.compile(r"^(false|0|no|fail|failed|failure|error|err|invalid|timeout|killed|cancel+ed|aborted)$", re.I)
PASSED = re.compile(r"^(true|1|yes|ok|pass|passed|success|succeeded|valid|done|completed|complete)$", re.I)


def _outcome(name: str, val: str) -> str:
    """failed | passed | unknown for one recorded status value."""
    v = val.strip()
    if name in ("returncode", "return_code", "exit_code", "exitcode", "rc"):
        try:
            return "passed" if int(float(v)) == 0 else "failed"
        except ValueError:
            return "unknown"
    if name in ("error", "errors"):
        return "passed" if v in ("", "None", "none", "null", "0", "[]") else "failed"
    if name == "failed":
        return "failed" if PASSED.match(v) else "passed" if FAILED.match(v) else "unknown"
    return "failed" if FAILED.match(v) else "passed" if PASSED.match(v) else "unknown"


def status(s, k, v, cat, outs, ctx):
    """What failed or passed: every recorded status-like value (outputs and attributes), by name and value, failures first."""
    names = [n for n in (s.get("names") or STATUS_NAMES)]
    rows = v.q("the recorded outcome values (return codes, status flags, errors) of outputs and attributes",
               f'.filter(col("name") == "EH" & (args.uri like "<name>=%" for {", ".join(names)}))',
               lambda tv, col, cols: tv.filter((col("name") == "EH") & _any_like(col, names)))
    by: Dict[tuple, Counter] = defaultdict(Counter)
    who: Dict[tuple, set] = defaultdict(set)
    cites: List[Dict[str, Any]] = []
    for r in rows:
        t, u = str(r.get("args.type", "")), str(r.get("args.uri", ""))
        n, _, val = u.partition("=")
        if n not in names or not (t.endswith(".output") or t.endswith(".attribute")):
            continue
        owner = t.rsplit(".", 1)[0]
        oc = _outcome(n, val)
        by[(owner, n, oc)][val] += 1
        if oc == "failed" and len(cites) < 40:
            cites.append(C.cite(r))
            who[(owner, n, oc)].add(val)
    order = {"failed": 0, "unknown": 1, "passed": 2}
    items = sorted(by.items(), key=lambda x: (order[x[0][2]], -sum(x[1].values())))
    out = [[o, n, oc, sum(c.values()), ", ".join(f"{val or '(empty)'} ×{m}" for val, m in c.most_common(6))] for (o, n, oc), c in items]
    nf = sum(sum(c.values()) for (o, n, oc), c in by.items() if oc == "failed")
    npass = sum(sum(c.values()) for (o, n, oc), c in by.items() if oc == "passed")
    head = ((f"{nf} recorded outcome(s) failed and {npass} passed" + (f"; failures in {', '.join(sorted({o + '.' + n for (o, n, oc) in by if oc == 'failed'}))[:200]}." if nf else "."))
            if by else "No outcome (return code, status, error) is recorded: whether each step passed is not in the provenance.")
    gaps = [] if by else [{"kind": "not_recorded", "subject": "step outcomes",
                           "detail": "no return code, status or error is captured: capture each step's outcome as an output (prov_decision)"}]
    from . import result
    return result(ASPECT, head, ["of", "value name", "outcome", "records", "values"], out, {"names": names}, cites, gaps=gaps,
                  values={"failed": nf, "passed": npass})


def _any_like(col, names):
    q = None
    for n in names:
        e = col("args.uri").like(f"{n}=%")
        q = e if q is None else (q | e)
    return q


OPS = {"status": status}
OPWORD = {"status": "count what failed and what passed"}
