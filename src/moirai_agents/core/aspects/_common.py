"""What the aspect operations share: merging per-file TraceViewer aggregates, the trace's own lookup tables (hosts, files, strings), and
formatting. Reads only through the evidence engine's Viewer (core.provtrace underneath)."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Tuple

ACTIVITY_CATS = ("moirai", "PROV")
PROV_NAMES = ("ET", "EH", "ER")
IO_CATS = ("POSIX", "STDIO")                 # one event per call
IO_AGG_CATS = ("posix", "stdio")             # dftracer's selective aggregation of small calls: args.dft_cnt calls, args.dur time
SYSTEM_CATS = ("sys", "io", "net", "mem")    # psutil counters (ph 2)
META_CATS = ("dftracer",)
NOT_APP = set(ACTIVITY_CATS) | set(IO_CATS) | set(IO_AGG_CATS) | set(SYSTEM_CATS) | set(META_CATS)
READS = ("read", "pread", "pread64", "readv", "preadv", "preadv2", "fread", "fgets", "getline")
WRITES = ("write", "pwrite", "pwrite64", "writev", "pwritev", "pwritev2", "fwrite", "fputs", "fprintf")


def num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def merge(rows: Iterable[Dict[str, Any]], keys: Tuple[str, ...], sums: Tuple[str, ...] = (), mins: Tuple[str, ...] = (),
          maxs: Tuple[str, ...] = ()) -> Dict[Tuple, Dict[str, float]]:
    """Per-file group-by rows into one table: counts and sums add, min/max fold."""
    out: Dict[Tuple, Dict[str, float]] = {}
    for r in rows:
        k = tuple(r.get(x) for x in keys)
        d = out.setdefault(k, {})
        for c in sums:
            d[c] = d.get(c, 0.0) + num(r.get(c))
        for c in mins:
            if r.get(c) is not None:
                d[c] = min(d.get(c, num(r[c])), num(r[c]))
        for c in maxs:
            if r.get(c) is not None:
                d[c] = max(d.get(c, num(r[c])), num(r[c]))
    return out


def lookups(v, ctx: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """The trace's own tables, read once per answer: hosts (HH: hash -> name), files (FH: hash -> path), strings (SH: hash -> text)."""
    if "lookups" in ctx:
        return ctx["lookups"]
    rows = v.q("the trace's own tables: host names (HH), file paths (FH), strings such as command lines (SH)",
               '.filter(col("name").is_in(["HH", "FH", "SH"]))', lambda tv, col, cols: tv.filter(col("name").is_in(["HH", "FH", "SH"])))
    t: Dict[str, Dict[str, str]] = {"HH": {}, "FH": {}, "SH": {}}
    for r in rows:
        if r.get("name") in t and r.get("args.value"):
            t[r["name"]][str(r["args.value"])] = str(r.get("args.name", ""))
    ctx["lookups"] = t
    return t


def host(lk, h) -> str:
    return lk["HH"].get(str(h), str(h or "?"))


def secs(us: float) -> str:
    s = us / 1e6
    return f"{s:,.3f} s" if s < 100 else f"{s:,.1f} s"


def size(b: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(b) < 1024 or u == "TiB":
            return f"{b:,.0f} {u}" if u == "B" else f"{b:,.2f} {u}"
        b /= 1024
    return f"{b:,.2f} TiB"


def pct(a: float, b: float) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "-"


def iso(us: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(us / 1e6)) + " UTC" if us else "-"


def cite(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"rec": r.get("name", ""), "file": r.get("_file", ""), "pid": r.get("pid", ""), "ts": r.get("ts") or r.get("min_ts") or 0, "id": r.get("args.id", "")}


def short(path: str, n: int = 70) -> str:
    return path if len(path) <= n else "…" + path[-(n - 1):]


def base(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path
