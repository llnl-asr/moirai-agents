"""Accountability: who did what, when, and with what. The person who ran it and their group, the job, the hosts, every process (its parent,
start, executable, command line, working directory), the tools and their versions (dftracer's start/end records: version, build, config),
and when each activity ran. The person, group and job are recorded by the annotation as a run_context entity (prov_annotate's helper); the
rest comes from dftracer's own metadata in the same traces (HH, SH, FH, start, end)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from . import _common as C

ASPECT, LABEL = "accountability", "Accountability"
SCOPE = ("Who did what, when and with what: the person running and their group, the job, the hosts and processes (with their parent, "
         "start time, executable, command line and working directory), the app and its functions, the tools and versions that took part, "
         "and the temporal order of the work.")
CORE_OPS: tuple = ()
CAPTURE = ("a run_context entity with user, group, job id, host, launch command, app version and tool versions (prov_annotate's helper "
           "records it at start); dftracer metadata on (HH, SH, FH, start and end records).")
CONTEXT_TYPE = "run_context"
IDENTITY_TYPE = "identity"           # the names behind the hashes: dropped to anonymize a trace
META_KEYS = ("user_hash", "group_hash", "job", "user", "group")   # dftracer process metadata (CM records) that may carry them
FIELDS = ("user", "group", "job", "host", "command", "app_version", "moirai", "dftracer", "python", "cwd")


def _context_entities(v) -> Dict[str, List[str]]:
    """The run_context attribute values the annotation recorded: {field: [values]}."""
    rows = v.q("the run context the annotation recorded (who ran it, group, job, versions)", f'.filter(col("name") == "EH" & col("args.type").like("{CONTEXT_TYPE}%"))',
               lambda tv, col, cols: tv.filter((col("name") == "EH") & col("args.type").like(f"{CONTEXT_TYPE}%")))
    out: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        u = str(r.get("args.uri", ""))
        if "=" in u:
            k, _, val = u.partition("=")
            if val not in out[k]:
                out[k].append(val)
    return out


def _identities(v) -> Dict[str, str]:
    rows = v.q("the names behind the identity hashes (absent once the trace is anonymized)", f'.filter(col("name") == "EH" & col("args.type") == "{IDENTITY_TYPE}")',
               lambda tv, col, cols: tv.filter((col("name") == "EH") & (col("args.type") == IDENTITY_TYPE)))
    return {h: n for h, _, n in (str(r.get("args.uri", "")).partition("=") for r in rows) if h}


def _process_metadata(v, ctxv) -> None:
    rows = v.q("process metadata dftracer recorded (CM): user/group hashes, job", f'.filter(col("name") == "CM" & col("args.name").is_in({list(META_KEYS)}))',
               lambda tv, col, cols: tv.filter((col("name") == "CM") & col("args.name").is_in(list(META_KEYS))))
    for r in rows:
        key, val = str(r.get("args.name", "")), str(r.get("args.value", ""))
        if val and val not in ctxv.get(key, []):
            ctxv.setdefault(key, []).append(val)


def _resolve(ctxv, ids, lk, kind) -> List[str]:
    """Names for a kind (user, group): plain values, then each hash through the identity records or dftracer's string table, else 'anonymized'."""
    out = list(ctxv.get(kind, []))
    for h in ctxv.get(f"{kind}_hash", []):
        name = ids.get(h) or lk["SH"].get(h)
        out.append(name if name else f"anonymized {kind} {h}")
    return list(dict.fromkeys(out))


def context(s, k, v, cat, outs, ctx):
    """Who ran it, where, as which processes, with which tools. User and group are kept as hashes; their names come from the identity
    records (or dftracer's string table), which anonymization removes."""
    lk = C.lookups(v, ctx)
    ctxv = _context_entities(v)
    _process_metadata(v, ctxv)
    ids = _identities(v)
    starts = v.q("each process's start record: parent, executable, command, working directory, date, dftracer version", '.filter(col("name") == "start")',
                 lambda tv, col, cols: tv.filter(col("name") == "start"))
    ends = v.q("each process's end record: dftracer build and configuration", '.filter(col("name") == "end")',
               lambda tv, col, cols: tv.filter(col("name") == "end"))
    build = {}
    for r in ends[:1]:
        build = {kk[len("args.build."):]: vv for kk, vv in r.items() if kk.startswith("args.build.") and vv}
    rows: List[List[Any]] = []
    for r in sorted(starts, key=lambda x: C.num(x.get("ts")))[:60]:
        rows.append([C.host(lk, r.get("hhash")), r.get("pid", ""), r.get("args.ppid", ""), r.get("args.date", ""),
                     C.short(lk["SH"].get(str(r.get("args.exec_hash")), ""), 50), C.short(lk["SH"].get(str(r.get("args.cmd_hash")), "").replace(";", " "), 90),
                     C.short(lk["FH"].get(str(r.get("args.cwd")), ""), 50), r.get("args.version", "")])
    hosts = sorted(set(lk["HH"].values()))
    for r in starts:                           # dftracer may record who started each process in its start record (args.user/group/job)
        for f in ("user", "group", "job", "user_hash", "group_hash"):
            val = r.get(f"args.{f}")
            if val not in (None, "") and str(val) not in ctxv.get(f, []):
                ctxv.setdefault(f, []).append(str(val))
    users, groups = _resolve(ctxv, ids, lk, "user"), _resolve(ctxv, ids, lk, "group")
    ctxv["users"], ctxv["groups"] = users, groups
    who = ", ".join(users) or None
    head = (f"Run by {who}" + (f" (group {', '.join(groups)})" if groups else "") + (f", job {', '.join(ctxv['job'])}" if ctxv.get("job") else "")
            if who else "Who ran it is not recorded")
    head += (f"; {len(starts)} process(es) on {len(hosts)} host(s) ({', '.join(hosts[:4])}{' …' if len(hosts) > 4 else ''})"
             + (f", from {min(r.get('args.date', '') for r in starts)}" if starts else "") + ".")
    tools = {f: ", ".join(ctxv[f]) for f in ("app_version", "moirai", "dftracer", "python") if ctxv.get(f)}
    if starts:
        tools.setdefault("dftracer (trace format)", str(starts[0].get("args.version", "")))
    if tools:
        head += " Tools: " + "; ".join(f"{a} {b}" for a, b in tools.items()) + "."
    gaps = [] if who else [{"kind": "not_recorded", "subject": CONTEXT_TYPE,
                            "detail": "who ran it, their group and the job are not in the provenance: the annotation records no run_context entity (prov_accountability)"}]
    from . import result
    return result(ASPECT, head, ["host", "pid", "parent", "started", "executable", "command", "working dir", "dftracer"], rows,
                  {"context": dict(ctxv), "build": build}, [C.cite(r) for r in starts[:20]], gaps=gaps, values={"context": dict(ctxv), "hosts": hosts})


def timeline(s, k, v, cat, outs, ctx):
    """When each activity ran (first and last start, runs, hosts), in time order. name: one activity only."""
    lk = C.lookups(v, ctx)
    nm = s.get("name")
    rows = v.q("when each activity ran, per host", f'.filter(col("cat").is_in({list(C.ACTIVITY_CATS)}){" & name == " + repr(nm) if nm else ""}).group_by("name", "hhash").agg(count, min ts, max ts)',
               lambda tv, col, cols: tv.filter((col("cat").is_in(list(C.ACTIVITY_CATS)) & ~col("name").is_in(list(C.PROV_NAMES)) & (col("name") == nm))
                                               if nm else (col("cat").is_in(list(C.ACTIVITY_CATS)) & ~col("name").is_in(list(C.PROV_NAMES))))
               .group_by("name", "hhash").agg(col("ts").count(), col("ts").min(), col("ts").max()))
    m = C.merge(rows, ("name", "hhash"), sums=("count",), mins=("min_ts",), maxs=("max_ts",))
    per: Dict[str, Dict[str, Any]] = {}
    for (n, h), d in m.items():
        e = per.setdefault(n, {"n": 0.0, "t0": float("inf"), "t1": 0.0, "hosts": set()})
        e["n"] += d.get("count", 0.0)
        e["t0"] = min(e["t0"], d.get("min_ts", e["t0"]))
        e["t1"] = max(e["t1"], d.get("max_ts", 0.0))
        e["hosts"].add(C.host(lk, h))
    items = sorted(per.items(), key=lambda x: x[1]["t0"])
    out = [[n, f"{int(e['n']):,}", C.iso(e["t0"]), C.iso(e["t1"]), ", ".join(sorted(e["hosts"]))[:80]] for n, e in items]
    head = (f"{len(items)} activit{'y' if len(items) == 1 else 'ies'} from {C.iso(items[0][1]['t0'])} to {C.iso(max(e['t1'] for _, e in items))}; first "
            f"{items[0][0]}, last to start {max(items, key=lambda x: x[1]['t0'])[0]}." if items else "No activity is recorded" + (f" named {nm}." if nm else "."))
    from . import result
    return result(ASPECT, head, ["activity", "runs", "first start", "last start", "hosts"], out, {"name": nm}, [])


def configuration(s, k, v, cat, outs, ctx):
    """How each process was configured: the process metadata moirai records at start (CM: env.<NAME> settings, moirai.*, the app's own
    app.* values). match: only keys containing it. The user's name masked in a value (@user:<hash>) is shown only while the identity records
    are there."""
    lk = C.lookups(v, ctx)
    ids = _identities(v)
    match = str(s.get("match") or "")
    rows = v.q("each process's recorded configuration (moirai's process metadata, CM records)", '.filter(col("name") == "CM")',
               lambda tv, col, cols: tv.filter(col("name") == "CM"))
    per: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    procs = set()
    for r in rows:
        key, val = str(r.get("args.name", "")), str(r.get("args.value", ""))
        if not key or key in ("time_metric",) or (match and match.lower() not in key.lower()):
            continue
        for h, name in ids.items():
            val = val.replace(f"@user:{h}", name)
        per[key][val].add((C.host(lk, r.get("hhash")), r.get("pid", "")))
        procs.add((r.get("hhash"), r.get("pid")))
    out = [[key, val, len(where), ", ".join(sorted({h for h, _ in where}))[:60]] for key in sorted(per) for val, where in sorted(per[key].items())]
    env = sorted({k for k in per if k.startswith("env.")})
    head = (f"{len(per)} configuration setting(s) recorded for {len(procs)} process(es)" + (f", {len(env)} of them environment ({', '.join(e[4:] for e in env[:6])}{' …' if len(env) > 6 else ''})" if env else "")
            + ("; settings that differ between processes: " + ", ".join(k for k in per if len(per[k]) > 1)[:200] if any(len(x) > 1 for x in per.values()) else "") + "."
            if per else "No process configuration is recorded: moirai records it at initialize (process metadata) from version with moirai.metadata.")
    from . import result
    return result(ASPECT, head, ["setting", "value", "processes", "hosts"], out, {"match": match}, [C.cite(r) for r in rows[:20]],
                  gaps=[] if per else [{"kind": "not_recorded", "subject": "process configuration",
                                        "detail": "no process metadata (CM records): run with a moirai that records it at initialize"}],
                  values={"settings": len(per)})


OPS = {"context": context, "timeline": timeline, "configuration": configuration}
OPWORD = {"context": "find who ran it, where, and with what", "timeline": "put the activities in time order",
          "configuration": "read how each process was configured"}
