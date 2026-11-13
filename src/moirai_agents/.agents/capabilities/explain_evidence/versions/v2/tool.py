"""Explain the evidence of a computed answer: where it came from, first in a sentence (which engine, how many files, queries and records,
how many records are cited, the result's hash), then the files read, the queries as run and the cited records, as tables."""
import time

from moirai_agents.core import explain as X


def _size(b) -> str:
    try:
        b = float(b)
    except (TypeError, ValueError):
        return "-"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return "-"


def explain(answer: dict) -> dict:
    st = answer.get("stats") or {}
    reads = answer.get("reads") or []
    qs = answer.get("queries") or []
    steps = answer.get("steps") or []
    eng = answer.get("engine_info") or {}
    cited = sum(int(s.get("cited") or 0) for s in steps)
    summary = (f"Computed by {eng.get('name', 'TraceViewer')} {eng.get('version', '')}".rstrip() +
               f" from {X.plural(len(reads), 'trace file')}: {X.plural(len(qs), 'query', 'queries')} read "
               f"{X.plural(int(st.get('rows_read') or 0), 'record')}, {X.plural(cited, 'record')} cited. "
               f"Re-running the plan on the same traces gives result {answer.get('result_hash', '?')} again.")
    facts = [{"label": "files", "value": len(reads)}, {"label": "queries", "value": len(qs)},
             {"label": "records read", "value": int(st.get("rows_read") or 0)}, {"label": "cited", "value": cited}]
    details = [
        {"title": f"Files read ({len(reads)})", "blocks": [X.table(["file", "size", "sha256 of its ends", "modified"],
                                                                   [[r.get("file", ""), _size(r.get("bytes")), r.get("sha256_ends", ""),
                                                                     time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(r["mtime"])) if r.get("mtime") else "-"]
                                                                    for r in reads])]},
        {"title": f"Queries ({len(qs)})", "blocks": [X.block("query", label=f"Q{q.get('n')}", why=q.get("why", ""), code=q.get("query", ""),
                                                             meta=f"{X.plural(int(q.get('rows') or 0), 'row')} · {q.get('secs', '?')} s")
                                                     for q in qs] or [X.block("note", tone="warn", text="no query recorded")]},
    ]
    for s in steps:
        cs = s.get("cites") or []
        if not cs:
            continue
        rows = [[c.get("rec", ""), c.get("file", ""), str(c.get("pid", "")), X.fmt_num(c["ts"]) if isinstance(c.get("ts"), (int, float)) and c["ts"] else "-",
                 c.get("id", "")] for c in cs]
        t = X.table(["record", "file", "pid", "time", "id"], rows, total=int(s.get("cited") or len(cs)))
        t["numeric"] = []
        details.append({"title": f"S{s.get('step')}: {X.plural(int(s.get('cited') or len(cs)), 'record')} cited", "pick": f"s{s.get('step')}", "blocks": [t]})
    return X.part("evidence", "Evidence", summary, facts, details)


def register(mcp):
    @mcp.tool()
    def explain_evidence(answer: dict) -> dict:
        """Explain the evidence of a computed answer: a summary of how it was computed (engine, files, queries, records read and cited, the
        result hash), then the files read, the queries as run and each step's cited records as table and query blocks.
        Returns {part: evidence, title, summary, facts, details}."""
        return explain(answer)

    @mcp.tool()
    def explain_evidence_check(answer: dict) -> dict:
        """Validator: the evidence explanation is well formed, its numbers come from the answer, and every query and file read is listed.
        Returns {status: pass|fail, problems}."""
        p = explain(answer)
        bad = X.check(p, answer, "evidence")
        nq = sum(1 for d in p["details"] for b in d["blocks"] if b.get("type") == "query")
        if nq != len(answer.get("queries") or []):
            bad.append(f"details: {nq} query blocks for {len(answer.get('queries') or [])} queries")
        return {"status": "fail" if bad else "pass", "problems": bad}
