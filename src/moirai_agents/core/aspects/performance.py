"""Performance: how the run can be more efficient. Where the time went (per provenance activity, per annotated app function, per I/O call,
per process and host, per layer: app-centric vs system-centric), how much I/O moved and how fast, how parallel the run was, and what the
node did meanwhile (psutil CPU, disk and network counters). Everything is a TraceViewer group-by on dftracer's own events in the same
traces (POSIX/STDIO calls, the aggregated small calls, app function events, sys/io/net counters) and the activities' durations."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from . import _common as C

ASPECT, LABEL = "performance", "Performance"
SCOPE = ("How to improve efficiency: the bottleneck (where time goes, by activity, function, I/O call, process, host), I/O volume, time and "
         "bandwidth per file and operation, parallelism (processes, hosts, how much of the wall time ran concurrently), and app-centric vs "
         "system-centric time (the app's own work vs the I/O and system calls under it, and the node's CPU, disk and network counters).")
CORE_OPS: tuple = ()
CAPTURE = ("dftracer I/O tracing on (MOIRAI_TRACE_IO=1: POSIX/STDIO calls with bytes and time), its metadata on (FH/HH/SH/start/end records), "
           "psutil system counters, the activities' own durations, and dftracer function annotation of the app's hot code (regions the "
           "provenance activities do not cover).")
TOP = 15


def _io_rows(v, by: str) -> Dict[tuple, Dict[str, float]]:
    """Per-call I/O (POSIX/STDIO) grouped by `by` and the call name, plus the aggregated small calls (posix/stdio) for counts and time."""
    keys = (by, "name") if by != "name" else ("name",)
    rows = v.q(f"I/O calls by {by}: calls, bytes (ret), time", f'.filter(col("cat").is_in({list(C.IO_CATS)})).group_by({", ".join(map(repr, keys))}).agg(count, sum dur, sum args.ret)',
               lambda tv, col, cols: tv.filter(col("cat").is_in(list(C.IO_CATS))).group_by(*keys).agg(col("ts").count(), col("dur").sum(),
                                                                                                     *([col("args.ret").sum()] if "args.ret" in cols else [])))
    m = C.merge(rows, keys, sums=("count", "sum_dur", "sum_args.ret"))
    agg = v.q(f"aggregated small I/O calls by {by}: calls (dft_cnt), time (args.dur)", f'.filter(col("cat").is_in({list(C.IO_AGG_CATS)})).group_by({", ".join(map(repr, keys))}).agg(sum args.dft_cnt, sum args.dur)',
              lambda tv, col, cols: (tv.filter(col("cat").is_in(list(C.IO_AGG_CATS))).group_by(*keys).agg(col("args.dft_cnt").sum(), col("args.dur").sum())
                                     if "args.dft_cnt" in cols else None))
    for k, d in C.merge(agg, keys, sums=("sum_args.dft_cnt", "sum_args.dur")).items():
        e = m.setdefault(k, {"count": 0.0, "sum_dur": 0.0, "sum_args.ret": 0.0})
        e["count"] = e.get("count", 0.0) + d.get("sum_args.dft_cnt", 0.0)
        e["sum_dur"] = e.get("sum_dur", 0.0) + d.get("sum_args.dur", 0.0)
    return m


def _activity_time(v, by: str) -> Dict[tuple, Dict[str, float]]:
    rows = v.q(f"provenance activities by {by}: runs, total and span of time", f'.filter(col("cat").is_in({list(C.ACTIVITY_CATS)})).group_by({by!r}).agg(count, sum dur, min ts, max ts)',
               lambda tv, col, cols: tv.filter(col("cat").is_in(list(C.ACTIVITY_CATS)) & ~col("name").is_in(list(C.PROV_NAMES)))
               .group_by(by).agg(col("ts").count(), col("dur").sum(), col("ts").min(), col("ts").max()))
    return C.merge(rows, (by,), sums=("count", "sum_dur"), mins=("min_ts",), maxs=("max_ts",))


def _function_time(v) -> Dict[tuple, Dict[str, float]]:
    """dftracer function/region events of the app (any category that is not provenance, I/O, a counter or metadata)."""
    rows = v.q("app function and region events (dftracer annotation) by category and name", '.filter(~col("cat").is_in(<provenance, I/O, counters, metadata>)).group_by("cat", "name").agg(count, sum dur)',
               lambda tv, col, cols: tv.filter(~col("cat").is_in(sorted(C.NOT_APP))).group_by("cat", "name").agg(col("ts").count(), col("dur").sum()))
    return {k: d for k, d in C.merge(rows, ("cat", "name"), sums=("count", "sum_dur")).items() if d.get("sum_dur")}


def perf_time(s, k, v, cat, outs, ctx):
    """Where the time went. by: activity (default) | function | io | process | host | layer."""
    by = str(s.get("by") or "activity")
    top = int(s.get("top") or TOP)
    lk = C.lookups(v, ctx) if by in ("process", "host") else None
    rows: List[List[Any]] = []
    if by == "activity":
        m = _activity_time(v, "name")
        tot = sum(d["sum_dur"] for d in m.values())
        items = sorted(m.items(), key=lambda x: -x[1]["sum_dur"])
        rows = [[n[0], f"{int(d['count']):,}", C.secs(d["sum_dur"]), C.secs(d["sum_dur"] / max(d["count"], 1)), C.pct(d["sum_dur"], tot),
                 C.iso(d.get("min_ts", 0))] for n, d in items[:top]]
        head = (f"{len(m)} activities ran {int(sum(d['count'] for d in m.values())):,} times for {C.secs(tot)} in all; the most time went to "
                f"{items[0][0][0]} ({C.secs(items[0][1]['sum_dur'])}, {C.pct(items[0][1]['sum_dur'], tot)})." if items else "No provenance activity is recorded.")
        cols = ["activity", "runs", "total time", "mean time", "share", "first start"]
    elif by == "function":
        m = _function_time(v)
        tot = sum(d["sum_dur"] for d in m.values())
        items = sorted(m.items(), key=lambda x: -x[1]["sum_dur"])
        rows = [[n[1], n[0], f"{int(d['count']):,}", C.secs(d["sum_dur"]), C.pct(d["sum_dur"], tot)] for n, d in items[:top]]
        head = (f"{len(m)} annotated app functions/regions took {C.secs(tot)}; the most went to {items[0][0][1]} ({C.pct(items[0][1]['sum_dur'], tot)})."
                if items else "No dftracer function or region event of the app is recorded: the app's own code is not performance-annotated.")
        cols = ["function", "category", "calls", "total time", "share"]
    elif by == "io":
        m = _io_rows(v, "name")
        tot = sum(d["sum_dur"] for d in m.values())
        items = sorted(m.items(), key=lambda x: -x[1]["sum_dur"])
        moved = lambda n, d: C.size(d.get("sum_args.ret", 0)) if n in C.READS + C.WRITES else "-"      # noqa: E731  ret is bytes only for reads and writes
        rows = [[n[0], f"{int(d['count']):,}", C.secs(d["sum_dur"]), moved(n[0], d), C.pct(d["sum_dur"], tot)] for n, d in items[:top]]
        head = (f"{int(sum(d['count'] for d in m.values())):,} I/O calls took {C.secs(tot)}; the most time went to {items[0][0][0]} ({C.pct(items[0][1]['sum_dur'], tot)})."
                if items else "No I/O call is recorded: dftracer I/O tracing was off.")
        cols = ["call", "calls", "time", "bytes moved", "share"]
    elif by in ("process", "host"):
        key = "pid" if by == "process" else "hhash"
        a, io = _activity_time(v, key), _io_rows(v, key)
        per: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"act": 0.0, "io": 0.0, "n": 0.0})
        for (x,), d in a.items():
            per[x]["act"] += d["sum_dur"]
            per[x]["n"] += d["count"]
        for (x, _n), d in io.items():
            per[x]["io"] += d["sum_dur"]
        items = sorted(per.items(), key=lambda x: -(x[1]["act"] + x[1]["io"]))
        label = (lambda x: f"pid {x}") if by == "process" else (lambda x: C.host(lk, x))
        rows = [[label(x), f"{int(d['n']):,}", C.secs(d["act"]), C.secs(d["io"]), C.pct(d["io"], d["act"]) if d["act"] else "-"] for x, d in items[:top]]
        head = (f"{len(per)} {by}{'es' if by == 'process' else 's'}; the busiest is {label(items[0][0])} ({C.secs(items[0][1]['act'])} in activities, "
                f"{C.secs(items[0][1]['io'])} in I/O calls)." if items else f"No time is recorded per {by}.")
        cols = [by, "activity runs", "activity time", "I/O time", "I/O vs activity time"]
    elif by == "layer":
        a = _activity_time(v, "name")
        f = _function_time(v)
        io = _io_rows(v, "name")
        act, fun, iot = (sum(d["sum_dur"] for d in m.values()) for m in (a, f, io))
        rows = [["app: provenance activities", f"{int(sum(d['count'] for d in a.values())):,}", C.secs(act)],
                ["app: annotated functions/regions", f"{int(sum(d['count'] for d in f.values())):,}", C.secs(fun)],
                ["system: I/O calls (POSIX/STDIO)", f"{int(sum(d['count'] for d in io.values())):,}", C.secs(iot)]]
        head = (f"App-centric time: {C.secs(act)} in provenance activities and {C.secs(fun)} in annotated functions; system-centric: {C.secs(iot)} "
                f"in I/O calls ({C.pct(iot, act) if act else '-'} of the activity time).")
        cols = ["layer", "events", "time"]
    else:
        raise ValueError(f"step {k + 1} (perf_time): by must be activity, function, io, process, host or layer")
    from . import result
    gaps = [] if rows else [{"kind": "not_recorded", "subject": f"perf_time by {by}", "detail": head}]
    return result(ASPECT, head, cols, rows, {"by": by, "top": top}, [], gaps=gaps)


def perf_io(s, k, v, cat, outs, ctx):
    """I/O volume, time and bandwidth. by: file (default) | call | host."""
    by = str(s.get("by") or "file")
    key = {"file": "fhash", "call": "name", "host": "hhash"}.get(by)
    if not key:
        raise ValueError(f"step {k + 1} (perf_io): by must be file, call or host")
    top = int(s.get("top") or TOP)
    lk = C.lookups(v, ctx)
    m = _io_rows(v, key)
    per: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"n": 0.0, "t": 0.0, "r": 0.0, "w": 0.0})
    for kk, d in m.items():
        x, call = (kk[0], kk[-1])
        e = per[x]
        e["n"] += d.get("count", 0.0)
        e["t"] += d.get("sum_dur", 0.0)
        if call in C.READS:
            e["r"] += d.get("sum_args.ret", 0.0)
        elif call in C.WRITES:
            e["w"] += d.get("sum_args.ret", 0.0)
    name = {"file": lambda x: C.short(lk["FH"].get(str(x), f"file {x}")), "call": str, "host": lambda x: C.host(lk, x)}[by]
    nofile = per.pop(None, None) if by == "file" else None      # calls on no file (waitpid, munmap, ...): process calls, not file I/O
    items = sorted(per.items(), key=lambda x: -x[1]["t"])
    rows = [[name(x), f"{int(d['n']):,}", C.size(d["r"]), C.size(d["w"]), C.secs(d["t"]),
             (C.size((d["r"] + d["w"]) / (d["t"] / 1e6)) + "/s") if d["t"] and (d["r"] + d["w"]) else "-"] for x, d in items[:top]]
    tr, tw, tt = (sum(d[c] for d in per.values()) for c in ("r", "w", "t"))
    head = (f"{C.size(tr)} read and {C.size(tw)} written in {int(sum(d['n'] for d in per.values())):,} I/O calls taking {C.secs(tt)}"
            + (f"; the most I/O time is on {name(items[0][0])} ({C.pct(items[0][1]['t'], tt)})" if items else "")
            + (f"; {int(nofile['n']):,} more calls on no file (process calls such as waitpid, munmap) took {C.secs(nofile['t'])}" if nofile else "") + ".") if per or nofile else "No I/O call is recorded: dftracer I/O tracing was off."
    from . import result
    return result(ASPECT, head, [by, "calls", "read", "written", "time", "bandwidth"], rows, {"by": by, "top": top}, [],
                  gaps=[] if rows else [{"kind": "not_recorded", "subject": "I/O calls", "detail": "no POSIX/STDIO event: run with MOIRAI_TRACE_IO=1"}])


def perf_parallelism(s, k, v, cat, outs, ctx):
    """How parallel the run was: processes and hosts, the wall time, the busy time of the activities, and their average concurrency."""
    lk = C.lookups(v, ctx)
    rows = v.q("provenance activities by host and process: busy time and span", f'.filter(col("cat").is_in({list(C.ACTIVITY_CATS)})).group_by("hhash", "pid").agg(count, sum dur, min ts, max ts)',
               lambda tv, col, cols: tv.filter(col("cat").is_in(list(C.ACTIVITY_CATS)) & ~col("name").is_in(list(C.PROV_NAMES)))
               .group_by("hhash", "pid").agg(col("ts").count(), col("dur").sum(), col("ts").min(), col("ts").max()))
    m = C.merge(rows, ("hhash", "pid"), sums=("count", "sum_dur"), mins=("min_ts",), maxs=("max_ts",))
    ends = v.q("when each trace file's last activity ended (start + duration)", f'.filter(col("cat").is_in({list(C.ACTIVITY_CATS)})).with_column("end", ts + dur).select("end").max()',
               lambda tv, col, cols: tv.filter(col("cat").is_in(list(C.ACTIVITY_CATS)) & ~col("name").is_in(list(C.PROV_NAMES)))
               .with_column("end", col("ts") + col("dur")).select("end").max())
    end_of = {r["_file"]: C.num(r.get("end")) for r in ends if r.get("end") is not None}
    host_files: Dict[Any, set] = defaultdict(set)
    for r in rows:
        host_files[r.get("hhash")].add(r["_file"])
    hosts: Dict[Any, Dict[str, float]] = defaultdict(lambda: {"procs": 0.0, "busy": 0.0, "t0": float("inf"), "t1": 0.0})
    for (h, _p), d in m.items():
        e = hosts[h]
        e["procs"] += 1
        e["busy"] += d.get("sum_dur", 0.0)
        e["t0"] = min(e["t0"], d.get("min_ts", e["t0"]))
        e["t1"] = max(e["t1"], d.get("max_ts", 0.0), *(end_of.get(f, 0.0) for f in host_files[h]))
    t0 = min((e["t0"] for e in hosts.values()), default=0.0)
    t1 = max((e["t1"] for e in hosts.values()), default=0.0)
    wall, busy = max(t1 - t0, 0.0), sum(e["busy"] for e in hosts.values())
    rows_out = [[C.host(lk, h), int(e["procs"]), C.secs(e["busy"]), C.secs(max(e["t1"] - e["t0"], 0)),
                 f"{e['busy'] / (e['t1'] - e['t0']):.2f}" if e["t1"] > e["t0"] else "-"] for h, e in sorted(hosts.items(), key=lambda x: -x[1]["busy"])]
    head = (f"{len(m)} processes on {len(hosts)} host(s) ran activities for {C.secs(busy)} within {C.secs(wall)} of wall time: an average "
            f"concurrency of {busy / wall:.2f}." if wall else "No activity timing is recorded.") if m else "No provenance activity is recorded."
    from . import result
    return result(ASPECT, head, ["host", "processes", "busy time", "span", "average concurrency"], rows_out, {"processes": len(m), "hosts": len(hosts)},
                  [], values={"wall_us": wall, "busy_us": busy})


SYS = {"cpu": ("sys", "cpu", ("user_pct", "system_pct", "iowait_pct", "idle_pct"), "mean"),
       "disk": ("io", None, ("bytes_read", "bytes_written", "reads_completed", "writes_completed"), "sum"),
       "network": ("net", None, ("bytes_recv", "bytes_sent", "rx_errors", "tx_errors"), "sum")}


def perf_system(s, k, v, cat, outs, ctx):
    """What the nodes did meanwhile (system-centric): metric cpu (default) | disk | network, from dftracer's psutil counters, per host."""
    metric = str(s.get("metric") or "cpu")
    if metric not in SYS:
        raise ValueError(f"step {k + 1} (perf_system): metric must be one of {sorted(SYS)}")
    c, name, fields, how = SYS[metric]
    lk = C.lookups(v, ctx)
    rows = v.q(f"system {metric} counters per host ({how})", f'.filter(col("cat") == {c!r}{" & name == " + repr(name) if name else ""}).group_by("hhash").agg({how} of {", ".join(fields)})',
               lambda tv, col, cols: (tv.filter((col("cat") == c) & (col("name") == name) if name else (col("cat") == c)).group_by("hhash")
                                      .agg(col("ts").count(), *[getattr(col(f"args.{f}"), how)() for f in fields if f"args.{f}" in cols])
                                      if any(f"args.{f}" in cols for f in fields) else None))
    names = [f"{how}_args.{f}" for f in fields]
    per: Dict[Any, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        e = per[r.get("hhash")]
        n = C.num(r.get("count"))
        e["count"] += n
        for f in names:
            e[f] += C.num(r.get(f)) * (n if how == "mean" else 1)       # means are weighted by samples across files
    out = []
    for h, e in per.items():
        vals = [(e[f] / e["count"]) if how == "mean" and e["count"] else e[f] for f in names]
        out.append([C.host(lk, h), int(e["count"])] + [f"{x:.1f}%" if how == "mean" else (C.size(x) if "bytes" in f else f"{int(x):,}")
                                                        for x, f in zip(vals, fields)])
    from . import result
    head = (f"{metric} on {len(per)} host(s): " + "; ".join(f"{r[0]}: " + ", ".join(f"{f.replace('_pct', '')} {x}" for f, x in zip(fields, r[2:])) for r in out[:4]) + "."
            if out else f"No {metric} counter is recorded (dftracer's system counters were off).")
    return result(ASPECT, head, ["host", "samples", *fields], out, {"metric": metric}, [],
                  gaps=[] if out else [{"kind": "not_recorded", "subject": f"{metric} counters", "detail": head}])


OPS = {"perf_time": perf_time, "perf_io": perf_io, "perf_parallelism": perf_parallelism, "perf_system": perf_system}
OPWORD = {"perf_time": "find where the time went", "perf_io": "measure the I/O", "perf_parallelism": "measure how parallel the run was",
          "perf_system": "read the nodes' system counters"}
