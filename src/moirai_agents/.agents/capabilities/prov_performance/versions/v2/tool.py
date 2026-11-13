"""Performance (performance): how to improve efficiency: the bottleneck, time per function and layer, I/O, parallelism, app- vs system-centric. One of the four aspects of provenance (traceability, accountability, decision making, performance),
each its own capability. Its query operations live in moirai_agents.core.aspects.performance and are planned with evidence_answer; every read
goes through prov_read (core.provtrace, TraceViewer).

It also owns the performance ANNOTATION: the `performance` section of the annotation spec, which inserts dftracer's own Python API calls
(dftracer.python: ai.compute|data|comm|checkpoint|io regions and dft_fn) into the provenance copy of the app. perf_annotate plans the entries from
the traces, the spec and the source; perf_apply writes them (same copy as annotate_apply, idempotent); perf_validate checks them."""
import ast
import difflib
import json
import os
import re
from pathlib import Path

from moirai_agents.core import aspects as AS
from moirai_agents.core import pipeline as P

MARK = "moirai-agents:perf"
BEGIN, END = f"# {MARK} begin", f"# {MARK} end"
IMPORT_LINES = [
    f"from dftracer.python import ai as _dft_ai, dft_fn as _DftFn, dftracer as _dft_tracer  # {MARK}",
    f"if _dft_tracer.get_instance().logger is None:  # {MARK}",
    f"    _dft_tracer.initialize_log(logfile=None, data_dir=None, process_id=-1); __import__('atexit').register(_dft_tracer.get_instance().finalize)  # {MARK}",
]
AI_CATS = {"compute", "data", "comm", "checkpoint", "device", "dataloader", "pipeline", "other"}
CAT_RE = r"[A-Za-z_][A-Za-z0-9_\-]*"
COMP = {"io": "io", "comm": "comm", "mem": "data", "cpu": "compute"}
PRIORITY = ["checkpoint", "io", "comm", "data", "subprocess", "compute"]
SKIP_DIRS = {".git", "__pycache__", "build", ".venv", "venv", "node_modules", "tests", "test"}

# vocabularies (a trimmed copy of dftracer-agents' python_cost, used only when that module cannot be imported; the real one is preferred)
_CKPT = {"state_dict", "load_state_dict", "save_checkpoint", "load_checkpoint", "from_pretrained", "save_pretrained", "save_model", "load_model"}
_IO = {"open", "read", "write", "readlines", "writelines", "load", "save", "savez", "loadtxt", "savetxt", "fromfile", "tofile", "memmap", "read_csv",
       "read_parquet", "read_json", "read_hdf", "to_csv", "to_parquet", "to_hdf", "imread", "imwrite", "File", "create_dataset", "download", "upload"}
_COMM = {"all_reduce", "all_gather", "all_to_all", "reduce_scatter", "broadcast", "barrier", "send", "recv", "isend", "irecv", "init_process_group",
         "Send", "Recv", "Bcast", "Gather", "Scatter", "Allreduce", "Allgather", "Alltoall", "Barrier", "Sendrecv", "Waitall"}
_DATA = {"DataLoader", "collate_fn", "default_collate", "prefetch"}
_COMPUTE = {"backward", "step", "forward", "zero_grad", "synchronize", "predict", "infer", "inference", "run_model", "predict_structure", "fit", "train", "evaluate"}
_TRIVIAL = {"__repr__", "__str__", "__len__", "__eq__", "__ne__", "__hash__", "__format__", "__bool__", "__lt__", "__gt__", "__le__", "__ge__"}
_REC_CAT = {"io": "io", "checkpoint": "checkpoint", "comm": "comm", "data": "data", "compute": "compute", "lifecycle": "other", "generic": "compute"}


# ---------------------------------------------------------------- source helpers
def _called(n):
    f = n.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _functions(tree):
    out = []

    def walk(node, prefix=""):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}.{c.name}" if prefix else c.name
                out.append((c, q))
                walk(c, q)
            elif isinstance(c, ast.ClassDef):
                walk(c, f"{prefix}.{c.name}" if prefix else c.name)
            else:
                walk(c, prefix)
    walk(tree)
    return out


def _find_func(tree, ref):
    fs = _functions(tree)
    hit = [(n, q) for n, q in fs if q == ref]
    return hit or [(n, q) for n, q in fs if n.name == ref]


def _own(fn):
    """The nodes of a function's own body (not of nested functions, classes or lambdas, which are themselves listed)."""
    stack, out = list(fn.body), []
    while stack:
        n = stack.pop()
        out.append(n)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))
    return out


def _is_gen(fn):
    return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _own(fn))


def _parents(tree):
    p = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            p[id(c)] = n
    return p


def _siblings(tree, node):
    for n in ast.walk(tree):
        for f in ("body", "orelse", "finalbody"):
            lst = getattr(n, f, None)
            if isinstance(lst, list) and any(x is node for x in lst):
                return lst
    return None


def _loop_above(node, parents):
    p = parents.get(id(node))
    while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module, ast.Lambda)):
        if isinstance(p, (ast.For, ast.AsyncFor, ast.While)):
            return True
        p = parents.get(id(p))
    return False


def _hot(trees, name):
    """Called inside a loop somewhere in these trees."""
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, (ast.For, ast.AsyncFor, ast.While)) and any(isinstance(m, ast.Call) and _called(m) == name for m in ast.walk(n)):
                return True
    return False


def _multiline(node):
    for n in ast.walk(node):
        if isinstance(n, ast.JoinedStr) and getattr(n, "end_lineno", n.lineno) != n.lineno:
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes)) and getattr(n, "end_lineno", n.lineno) != n.lineno:
            return True
    return False


def _cands(scope, lines, anchor):
    """The statements of scope whose first line holds the anchor, in source order (definitions are not anchors)."""
    got = [n for n in ast.walk(scope) if isinstance(n, ast.stmt) and n is not scope and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
           and anchor in lines[n.lineno - 1]]
    return sorted(got, key=lambda n: (n.lineno, n.col_offset))


# ---------------------------------------------------------------- cost gating (dftracer-agents' python_estimate_function_cost, reused when importable)
def _local(fn, thr):
    c = {"io_calls": 0, "ckpt_calls": 0, "comm_calls": 0, "data_calls": 0, "compute_calls": 0, "loop_count": 0, "call_count": 0, "node_count": 0}
    for n in ast.walk(fn):
        c["node_count"] += 1
        if isinstance(n, ast.Call):
            c["call_count"] += 1
            nm = _called(n)
            for key, names in (("ckpt_calls", _CKPT), ("io_calls", _IO), ("comm_calls", _COMM), ("data_calls", _DATA), ("compute_calls", _COMPUTE)):
                if nm in names:
                    c[key] += 1
                    break
        elif isinstance(n, (ast.For, ast.While, ast.AsyncFor)):
            c["loop_count"] += 1
    c["score"] = (c["io_calls"] * 30 + c["ckpt_calls"] * 35 + c["comm_calls"] * 25 + c["data_calls"] * 20 + c["compute_calls"] * 10 + c["loop_count"] * 8
                  + c["call_count"] * 2 + min(c["node_count"] // 5, 20))
    sig = c["io_calls"] or c["ckpt_calls"] or c["comm_calls"]

    def out(rec, why, cat):
        return {"function": fn.name, "recommendation": rec, "reason": why, "category": cat, "score": c["score"], "threshold": thr, "cost_info": c}
    if not sig and fn.name in _TRIVIAL:
        return out("skip", "Rule 0: trivial dunder with no I/O", "trivial")
    if fn.name in ("__getitem__", "__iter__", "__next__"):
        return out("annotate", "data-pipeline dunder", "data")
    if c["ckpt_calls"]:
        return out("annotate", "checkpoint call present", "checkpoint")
    if c["io_calls"]:
        return out("annotate", "file/dataset I/O call present", "io")
    if c["comm_calls"]:
        return out("annotate", "communication call present", "comm")
    if len(fn.body) <= 5 and not c["loop_count"]:
        return out("skip", "Rule 0: trivial body, no loops, no I/O", "trivial")
    if c["score"] >= thr:
        return out("annotate", f"score {c['score']} >= threshold {thr}", "compute" if c["compute_calls"] else "generic")
    return out("skip", f"score {c['score']} < threshold {thr}", "cheap")


def _recommend(fn, thr=20):
    try:
        from dftracer_agents.mcp_tools.tools.session import python_cost as pc
        return pc.recommend(fn, thr)
    except Exception:  # noqa: BLE001  dftracer-agents not importable here: the trimmed local copy gives the same overrides
        return _local(fn, thr)


def _gate(node, trees, thr, force):
    """Why a function must not be decorated ('' when it may). Async functions and generators are refused always (a decorator would time the creation of
    the coroutine or generator, not its run); the cost gate and the hot-loop exclusion yield to force."""
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function: a decorator times the coroutine's creation, not its run; wrap an await region instead"
    if _is_gen(node):
        return "generator: a decorator times the creation of the generator, not its run; wrap the loop that consumes it"
    if force:
        return ""
    rec = _recommend(node, thr)
    if rec["recommendation"] == "skip":
        return "cost gate: " + rec["reason"]
    ci = rec.get("cost_info") or {}
    if not (ci.get("io_calls", 0) + ci.get("ckpt_calls", 0) + ci.get("comm_calls", 0)) and not ci.get("loop_count") and _hot(trees, node.name):
        return "hot-loop: called per iteration inside a loop and does no I/O, communication or loop of its own; annotate the loop that calls it"
    return ""


# ---------------------------------------------------------------- the dftracer calls
def _entry_cat(e):
    return str(e.get("category") or COMP.get(str(e.get("comp") or ""), ""))


def _ctx(cat, name):
    n = json.dumps(name)
    if cat == "io":
        return f"_dft_ai.data.io.derive({n})"
    if cat in AI_CATS:
        return f"_dft_ai.{cat}.derive({n})"
    return f"_DftFn({json.dumps(cat)}, name={n})"


def _deco(cat, name, init=False):
    if cat == "io" or cat in AI_CATS:
        return "@" + _ctx(cat, name)
    return f"@_DftFn({json.dumps(cat)}).{'log_init' if init else 'log'}(name={json.dumps(name)})"


def _strip(text):
    """The text without any performance insertion: idempotency of re-applying."""
    out, inside = [], False
    for ln in text.split("\n"):
        s = ln.strip()
        if s.endswith(BEGIN):
            inside = True
            continue
        if s.endswith(END):
            inside = False
            continue
        if s.endswith(MARK):
            continue
        out.append(ln[4:] if inside and s and ln.startswith("    ") else ln)
    return "\n".join(out)


def _region(tree, lines, parents, e, trees):
    """Resolve a region entry: {start, end, pad} or {problem} or {skip}."""
    scope = tree
    if e.get("in"):
        hit = _find_func(tree, e["in"])
        if len(hit) != 1:
            return {"problem": f"no function {e['in']}" if not hit else f"{e['in']} is ambiguous: write Class.method"}
        scope = hit[0][0]
    anchor, occ, count = str(e["anchor"]), int(e.get("occurrence") or 1), int(e.get("count") or 1)
    cands = _cands(scope, lines, anchor)
    if len(cands) < occ:
        return {"problem": f"anchor {anchor!r} not found (occurrence {occ}; {len(cands)} statement(s) match)"}
    first = cands[occ - 1]
    if lines[first.lineno - 1].lstrip().startswith("elif "):
        return {"problem": f"anchor {anchor!r} is an elif: anchor a statement inside it"}
    sib = _siblings(tree, first)
    idx = next(i for i, x in enumerate(sib) if x is first)
    chosen = sib[idx: idx + count]
    if len(chosen) < count:
        return {"problem": f"count {count} runs past the end of the block that holds {anchor!r}"}
    start, end = chosen[0].lineno, chosen[-1].end_lineno
    line0 = lines[start - 1]
    if len(line0) - len(line0.lstrip(" ")) != chosen[0].col_offset:
        return {"problem": f"anchor {anchor!r}: the statement shares its line or is indented with tabs; anchor another statement"}
    if any(_multiline(s) for s in chosen):
        return {"problem": f"anchor {anchor!r}: the range holds a multi-line string, which indenting would change; narrow the range"}
    if not e.get("force") and _loop_above(first, parents):
        return {"skip": "hot-loop: the statement runs per iteration of a loop; annotate the loop itself (anchor the `for`/`while`)"}
    return {"start": start, "end": end, "pad": " " * chosen[0].col_offset}


def perf_insert(text, entries, rel="", thr=20):
    """Insert the performance entries into one Python file's text (any earlier insertion is stripped first). Returns {text, applied, skipped, problems}."""
    base = _strip(text)
    res = {"text": base, "applied": [], "skipped": [], "problems": []}
    if not entries:
        return res
    try:
        tree = ast.parse(base)
    except SyntaxError as e:
        res["problems"].append(f"{rel}: does not parse (line {e.lineno}: {e.msg})")
        return res
    lines = base.split("\n")
    parents = _parents(tree)
    ops, regions, decos, seen = [], [], [], set()
    for e in entries:
        cat = _entry_cat(e)
        who = f"{rel}: {e.get('function') or e.get('anchor') or '?'}"
        if not re.fullmatch(CAT_RE, cat or ""):
            res["problems"].append(f"{who}: category must be a word (compute, data, comm, checkpoint, io, or your own), or comp io|comm|mem|cpu; got {cat!r}")
            continue
        if bool(e.get("function")) == bool(e.get("anchor")):
            res["problems"].append(f"{who}: an entry names a `function` (decorate it) or an `anchor` (wrap a statement range), not both and not neither")
            continue
        if e.get("function"):
            hit = _find_func(tree, e["function"])
            if len(hit) != 1:
                res["problems"].append(f"{who}: no function {e['function']} in the file" if not hit else f"{who}: ambiguous, write Class.method")
                continue
            node, qual = hit[0]
            if qual in seen:
                res["problems"].append(f"{who}: listed twice")
                continue
            seen.add(qual)
            why = _gate(node, [tree], int(e.get("threshold") or thr), bool(e.get("force")))
            if why:
                res["skipped"].append({"file": rel, "function": qual, "category": cat, "reason": why})
                continue
            name = str(e.get("name") or qual)
            decos.append(node.lineno)
            ops.append((node.lineno - 1, 1, "ins", [" " * node.col_offset + _deco(cat, name, node.name == "__init__") + "  # " + MARK]))
            res["applied"].append({"kind": "function", "file": rel, "target": qual, "category": cat, "name": name, "line": node.lineno})
        else:
            r = _region(tree, lines, parents, e, [tree])
            if r.get("problem"):
                res["problems"].append(f"{who}: {r['problem']}")
                continue
            if r.get("skip"):
                res["skipped"].append({"file": rel, "anchor": e["anchor"], "category": cat, "reason": r["skip"]})
                continue
            name = str(e.get("name") or f"{str(e.get('in') or 'module').split('.')[-1]}.{cat}")
            new = [f"{r['pad']}with {_ctx(cat, name)}:  {BEGIN}"] + [("    " + ln if ln.strip() else ln) for ln in lines[r["start"] - 1: r["end"]]] + [f"{r['pad']}{END}"]
            regions.append((r["start"], r["end"]))
            ops.append((r["start"] - 1, 1, "rep", (r["end"], new)))
            res["applied"].append({"kind": "region", "file": rel, "target": e.get("in") or "module", "anchor": e["anchor"], "category": cat, "name": name, "line": r["start"]})
    for (a, b), (c, d) in ((x, y) for i, x in enumerate(regions) for y in regions[i + 1:]):
        if a <= d and c <= b:
            res["problems"].append(f"{rel}: two regions overlap (lines {a}-{b} and {c}-{d})")
    for ln in decos:
        if any(a <= ln <= b for a, b in regions):
            res["problems"].append(f"{rel}: a decorated function (line {ln}) lies inside a wrapped region")
    if res["problems"]:
        res["applied"] = []
        return res
    if res["applied"]:
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        firsts = [min([d.lineno for d in getattr(n, "decorator_list", [])] + [n.lineno]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        first_def = min(firsts) if firsts else 10 ** 9
        before = [n.end_lineno for n in imports if n.lineno < first_def]
        doc = tree.body[0].end_lineno if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) \
            and isinstance(tree.body[0].value.value, str) else 0
        at = max(before) if before else doc
        if any(s - 1 < at < e for s, e in regions):
            res["problems"].append(f"{rel}: a wrapped region covers the import position")
            res["applied"] = []
            return res
        ops.append((at, 0, "ins", IMPORT_LINES))
    for pos, _prio, kind, payload in sorted(ops, key=lambda o: (o[0], o[1]), reverse=True):
        if kind == "ins":
            lines[pos:pos] = payload
        else:
            lines[pos:payload[0]] = payload[1]
    out = "\n".join(lines)
    try:
        ast.parse(out)
    except SyntaxError as e:
        res["problems"].append(f"{rel}: the inserted calls do not parse (line {e.lineno}: {e.msg}); this is a bug: report it with the entry")
        res["applied"] = []
        return res
    res["text"] = out
    return res


def _check_text(text, expect=None):
    """The checks on one performance-annotated file: [(name, ok, detail, fix)]."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [("parses", False, f"line {e.lineno}: {e.msg}", "fix the entry or re-run annotate_apply, then perf_apply")]
    lines = text.split("\n")
    imp = [ln for ln in lines if ln.startswith("from dftracer.python import") and ln.rstrip().endswith(MARK)]
    marked = any(MARK in ln for ln in lines)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("_dft_ai", "_DftFn", "_dft_tracer")}
    nb = sum(1 for ln in lines if ln.strip().endswith(BEGIN))
    ne = sum(1 for ln in lines if ln.strip().endswith(END))
    nd = sum(1 for ln in lines if re.match(r"\s*@_(dft_ai|DftFn)\b", ln) and ln.rstrip().endswith(MARK))
    rows = [("parses", True, "", ""),
            ("dftracer_imported_once", len(imp) == (1 if marked else 0), f"{len(imp)} dftracer import line(s) in the file", "perf_apply strips and re-inserts the block: run it again"),
            ("names_resolve", not used or bool(imp), f"{sorted(used)} used with no import", "perf_apply: the import block is inserted with the first call"),
            ("regions_balanced", nb == ne, f"{nb} begin, {ne} end markers", "perf_apply again; do not edit the markers")]
    if expect is not None:
        rows.append(("inserted", nd + nb == expect, f"{nd + nb} decorator(s)/region(s) in the file, {expect} applied", "perf_apply again"))
    return rows


# ---------------------------------------------------------------- planning
def _num(s):
    m = re.match(r"\s*([\d,]*\.?\d+)", str(s))
    return float(m.group(1).replace(",", "")) if m else 0.0


def _times(pipeline, traces):
    """perf_time by activity and by function on the pipeline's traces (through the evidence engine: TraceViewer)."""
    out = {"activities": {}, "functions": {}, "note": ""}
    try:
        from moirai_agents.core import evidence as E
        tr, idx = AS._traces(pipeline, traces)
        rows_a = E.run(tr, [{"op": "perf_time", "by": "activity"}], idx)["outs"][-1]["rows"]
        rows_f = E.run(tr, [{"op": "perf_time", "by": "function"}], idx)["outs"][-1]["rows"]
    except Exception as e:  # noqa: BLE001
        out["note"] = f"no trace times ({type(e).__name__}: {str(e)[:160]}); entries are planned from the source only"
        return out
    for r in rows_a:
        out["activities"][str(r[0])] = {"seconds": _num(r[2]), "share": _num(r[4])}
    for r in rows_f:
        out["functions"][f"{r[1]}/{r[0]}"] = {"seconds": _num(r[3]), "share": _num(r[4])}
    out["note"] = "the 15 largest activities and functions"
    return out


def _call_cat(call, app):
    f = call.func
    nm = _called(call)
    base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
    if nm in app and (isinstance(f, ast.Name) or base == "self"):
        return ""                                       # an app function: judged as a function, not by its name
    if (base == "subprocess" and nm in {"run", "call", "Popen", "check_call", "check_output"}) or nm in {"communicate", "waitpid"} \
            or (isinstance(f, ast.Attribute) and nm in {"wait", "system"}):
        return "subprocess"
    for cat, names in (("checkpoint", _CKPT), ("io", _IO), ("comm", _COMM), ("data", _DATA), ("compute", _COMPUTE)):
        if nm in names:
            return cat
    return ""


def _cats_in(node, app):
    found, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            c = _call_cat(n, app)
            if c:
                found.append((c, _called(n)))
        stack.extend(ast.iter_child_nodes(n))
    return found


def _best(found):
    return min(found, key=lambda x: PRIORITY.index(x[0])) if found else None


def _units(stmts, app):
    """The statements worth a region, outermost loop first (a loop is one unit, never its body: the hot-loop exclusion): [(stmt, category, call)]."""
    out = []
    for s in stmts:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
            b = _best(_cats_in(s, app))
            if b:
                out.append((s,) + b)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            b = _best([x for it in s.items for x in _cats_in(it.context_expr, app)])
            if b:
                out.append((s,) + b)
            else:
                out += _units(s.body, app)
        elif isinstance(s, ast.If):
            out += _units(s.body, app) + _units(s.orelse, app)
        elif isinstance(s, ast.Try):
            out += _units(s.body, app) + [u for h in s.handlers for u in _units(h.body, app)] + _units(s.orelse, app) + _units(s.finalbody, app)
        else:
            b = _best(_cats_in(s, app))
            if b:
                out.append((s,) + b)
    return out


def _plan(files, acts, times, max_entries=12, per_function=4, thr=20):
    trees = {}
    for rel, t in files.items():
        try:
            trees[rel] = ast.parse(t)
        except SyntaxError:
            continue
    defs = {}
    for rel, tr in trees.items():
        for n, q in _functions(tr):
            defs.setdefault(n.name, []).append((rel, q, n))
    app, tlist = set(defs), list(trees.values())
    resolved, targets = [], set()
    for a in acts:
        rel, ref = a.get("file", ""), a.get("function") or a.get("target") or a.get("in")
        if rel not in trees or not ref:
            continue
        hit = _find_func(trees[rel], ref)
        if len(hit) != 1:
            continue
        resolved.append((a, rel, hit[0][0], hit[0][1]))
        if a.get("mode", "wrap") in ("wrap", "patch"):
            targets.add((rel, hit[0][1]))
    entries, skipped, names, seen_f = [], [], set(), set()
    for a, rel, node, qual in resolved:
        t = times.get(a["name"]) or {}
        tag = {"activity": a["name"]}
        if t:
            tag.update(activity_seconds=t["seconds"], activity_share_pct=t["share"])
        lines = files[rel].split("\n")
        units = sorted(_units(node.body, app), key=lambda u: PRIORITY.index(u[1]))
        taken = 0
        for s, cat, call in units:
            anchor = lines[s.lineno - 1].strip()[:80]
            if taken >= per_function:
                skipped.append({"file": rel, "anchor": anchor, "reason": f"over per_function ({per_function}) in {qual}"})
                continue
            if _multiline(s):
                skipped.append({"file": rel, "anchor": anchor, "reason": "the statement holds a multi-line string"})
                continue
            cands = _cands(node, lines, anchor)
            if not any(x is s for x in cands):
                skipped.append({"file": rel, "anchor": anchor, "reason": "anchor text not found in the statement's first line"})
                continue
            nm = f"{qual.split('.')[-1]}.{call}"
            k = 2
            while (rel, nm) in names:
                nm, k = f"{qual.split('.')[-1]}.{call}_{k}", k + 1
            names.add((rel, nm))
            entries.append({"file": rel, "in": qual, "anchor": anchor, "occurrence": 1 + next(i for i, x in enumerate(cands) if x is s), "count": 1, "category": cat,
                            "name": nm, "why": f"{call}() in {qual} is {cat} work the activity {a['name']} hides", **tag})
            taken += 1
        for call in [m for m in _own(node) if isinstance(m, ast.Call)]:
            f, nm = call.func, _called(call)
            if isinstance(f, ast.Name):
                cand = defs.get(nm, [])
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self" and "." in qual:
                cand = [d for d in defs.get(nm, []) if d[0] == rel and d[1] == qual.rsplit(".", 1)[0] + "." + nm]
            else:
                continue
            if len(cand) != 1:
                continue
            hr, hq, hn = cand[0]
            if (hr, hq) in targets or (hr, hq) in seen_f:
                continue
            seen_f.add((hr, hq))
            why = _gate(hn, tlist, thr, False)
            if why:
                skipped.append({"file": hr, "function": hq, "reason": why})
                continue
            rec = _recommend(hn, thr)
            entries.append({"file": hr, "function": hq, "category": _REC_CAT.get(rec.get("category"), "compute"),
                            "why": f"{hq} ({rec['reason']}) is called by {qual}, activity {a['name']}", **tag})
    entries.sort(key=lambda e: (-(e.get("activity_share_pct") or 0), PRIORITY.index(e["category"]) if e["category"] in PRIORITY else 9))
    for e in entries[max_entries:]:
        skipped.append({"file": e["file"], "function": e.get("function"), "anchor": e.get("anchor"), "reason": f"over max_entries ({max_entries})"})
    return {"entries": entries[:max_entries], "skipped": skipped}


def _read_tree(root, limit=400):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(fn):
            if f.endswith(".py") and f != "_moirai_prov.py":
                p = Path(dp) / f
                try:
                    out[str(p.relative_to(root))] = p.read_text(errors="replace")
                except OSError:
                    continue
                if len(out) >= limit:
                    return out
    return out


def _safe(rel):
    p = Path(str(rel))
    return bool(str(rel)) and not p.is_absolute() and ".." not in p.parts


def _entries_of(spec):
    perf = (spec or {}).get("performance")
    if isinstance(perf, dict):
        perf = perf.get("entries")
    return [e for e in (perf or []) if isinstance(e, dict)]


SAMPLE = '''"""sample app"""
import os
import subprocess


def load(path):
    with open(path) as f:
        return f.read()


def tiny(x):
    return x + 1


class Runner:
    def run(self, items):
        out = []
        for it in items:
            out.append(tiny(it))
        subprocess.run(["true"], check=False)
        data = load("x")
        return out, data
'''
SAMPLE_ENTRIES = [
    {"file": "app.py", "function": "Runner.run", "category": "compute"},
    {"file": "app.py", "function": "load", "category": "io"},
    {"file": "app.py", "function": "tiny", "category": "compute"},
    {"file": "app.py", "in": "Runner.run", "anchor": "subprocess.run", "category": "subprocess", "name": "run.wait"},
]


def register(mcp):
    @mcp.tool()
    def performance_summary(pipeline: str = "", traces: str = "") -> dict:
        """The performance questions every run should answer, on a pipeline's traces (or a trace dir), through TraceViewer: time per activity, app vs system time, I/O per file, parallelism (processes, hosts, wall time, average concurrency), and the nodes' CPU counters.
        Use it to see what the provenance says about performance; for a user's question plan evidence_answer with the operations in the skill.
        Returns {aspect, label, scope, traces, answers: [{plan, headline, table, gaps}]}."""
        try:
            r = AS.summary("performance", pipeline, traces)
            r.pop("_outs", None)
            return r
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def performance_check(pipeline: str = "", traces: str = "") -> dict:
        """Validator: whether the annotation captured what performance needs (activities timed, I/O traced, system counters, app hot code performance-annotated). Each check names what is missing and the fix (the
        annotation change). Use after the baseline (or any run) and after re-annotating. Returns {aspect, status: pass|warn|fail, failed, advice,
        checks: [{name, ok, required, detail, fix}], capture}."""
        try:
            return AS.check("performance", pipeline, traces)
        except (P.PipelineError, ValueError, OSError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def performance_selftest() -> dict:
        """Self-check: the performance summary and check on a synthetic trace that holds every aspect must state the expected facts. Run after any
        change to this capability or to core.aspects.performance. Returns {status, missing, check, failed, line}."""
        return AS.selftest("performance")

    @mcp.tool()
    def perf_annotate(pipeline: str = "", traces: str = "", spec: dict = None, source: str = "", max_entries: int = 12, per_function: int = 4,
                      threshold: int = 20, write: bool = False) -> dict:
        """PLAN the `performance` section of the annotation spec: which of the app's functions and statement ranges to wrap with dftracer's own Python API
        (ai.compute|data|comm|checkpoint|io, dft_fn). Use at the annotate stage after performance_check advises app_functions_annotated, and show the result at the
        annotate gate; nothing is written to the app. It reads (1) perf_time by activity and by function on the pipeline's traces (the time the provenance
        activities hold and what is already dftracer-annotated), (2) the spec's activities (their functions: not decorated twice), (3) the source (the annotated
        copy <session>/provenance/source if it exists, else the user's source): inside each activity's function the statements that are subprocess waits, I/O,
        checkpoint, communication, data or compute work become region entries (a loop is one region, never its body), and the app functions it calls become
        function entries, both cost-gated with dftracer-agents' python_estimate_function_cost (skip trivial, hot-loop exclusion; async and generators refused).
        pipeline: the run pipeline id (spec, source and traces come from it). traces: a trace dir instead of the pipeline's. spec: a spec dict instead of the saved one.
        source: an app root instead of the pipeline's. max_entries: the most entries returned (ranked by the activity's share of time). per_function: the most regions per
        activity function. threshold: python_estimate_function_cost's score threshold. write: also save <session>/provenance/performance_plan.json.
        Returns {status, entries (send them as spec["performance"]), skipped: [{reason}], times: {activities, functions, note}, uncovered: [{activity, seconds, share_pct,
        planned_entries}], headline, spec_patch} or {status: error, error}. An entry: {file, function} or {file, in?, anchor, occurrence?, count?}, category (compute|data|comm|
        checkpoint|io|device|dataloader|pipeline|other or any word), name?, force?"""
        try:
            spec = json.loads(spec) if isinstance(spec, str) else spec
            root, c = (Path(source) if source else None), None
            if pipeline:
                c = P.load(pipeline)
                spec = spec or (P.stage(c, "annotate")["outputs"] or {}).get("spec")
                w = P.work(c) / "source"
                root = root or (w if w.is_dir() else Path(P.source_of(c)))
            if root is None or not root.is_dir():
                return {"status": "error", "error": "give a pipeline (its source) or source=<app root>"}
            if not spec or not spec.get("activities"):
                return {"status": "error", "error": "no spec: pass spec, or a pipeline whose annotate_spec ran"}
            files = {r: _strip(t) for r, t in _read_tree(root).items()}
            times = _times(pipeline, traces) if (pipeline or traces) else {"activities": {}, "functions": {}, "note": "no traces given; entries are planned from the source only"}
            res = _plan(files, spec["activities"], times["activities"], int(max_entries), int(per_function), int(threshold))
            ents = res["entries"]
            tot = sum(d["seconds"] for d in times["activities"].values())
            ann = sum(d["seconds"] for d in times["functions"].values())
            unc = [{"activity": n, "seconds": d["seconds"], "share_pct": d["share"], "planned_entries": sum(1 for e in ents if e.get("activity") == n)}
                   for n, d in sorted(times["activities"].items(), key=lambda x: -x[1]["seconds"])]
            head = (f"{len(times['activities'])} activities = {tot:,.3f} s, {ann:,.3f} s in dftracer-annotated functions; {len(ents)} performance entr{'y' if len(ents) == 1 else 'ies'} planned, "
                    f"{len(res['skipped'])} skipped." if times["activities"] else f"{len(ents)} performance entries planned from the source ({times['note']}).")
            out = {"status": "ok", "headline": head, "entries": ents, "skipped": res["skipped"], "times": times, "uncovered": unc, "spec_patch": {"performance": ents}}
            if write and c is not None:
                (P.work(c) / "performance_plan.json").write_text(json.dumps(out, indent=1))
            return out
        except (P.PipelineError, ValueError, OSError, KeyError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def perf_preview(files: dict, entries: list) -> dict:
        """Try performance entries on Python files given as text, in memory, with no pipeline: the same insertion annotate_apply's performance pass does. Use to check an
        entry (function, region, category, cost gate) before sending the spec, and to reproduce a mis-generated pattern. files: {relative path: text}. entries: as in the
        spec's `performance` section: {file, function} or {file, in?, anchor, occurrence?, count?}, category, name?, force?.
        Returns {status: ok|error, files: {path: annotated text}, applied, skipped: [{reason}], problems, checks: [{file, name, ok, detail}], idempotent}."""
        files = json.loads(files) if isinstance(files, str) else files
        entries = json.loads(entries) if isinstance(entries, str) else entries
        out = {"status": "ok", "files": {}, "applied": [], "skipped": [], "problems": [], "checks": [], "idempotent": True}
        for rel, text in files.items():
            mine = [e for e in entries if e.get("file") == rel]
            r = perf_insert(text, mine, rel)
            again = perf_insert(r["text"], mine, rel)
            out["idempotent"] = out["idempotent"] and again["text"] == r["text"]
            out["files"][rel] = r["text"]
            out["applied"] += r["applied"]
            out["skipped"] += r["skipped"]
            out["problems"] += r["problems"]
            out["checks"] += [{"file": rel, "name": n, "ok": ok, "detail": d} for n, ok, d, _f in _check_text(r["text"], len(r["applied"]) if r["applied"] else None)]
        out["status"] = "error" if out["problems"] or not out["idempotent"] or any(not c["ok"] for c in out["checks"]) else "ok"
        return out

    @mcp.tool()
    def perf_apply(pipeline: str) -> dict:
        """Write the spec's `performance` section into the provenance copy <session>/provenance/source: dftracer's own Python calls (one import block per file;
        a decorator per function entry; a `with` region per statement range), after annotate_apply copied the source and inserted the provenance (annotate_apply
        calls this in the same pass; call it directly after a spec change). Idempotent: any earlier insertion is stripped first, so re-running changes nothing; the user's
        source is never touched. Cost-gated entries are skipped with the reason, never failed. Never hand-edit the copy.
        Returns {status, files, applied, skipped, problems, diff} (status error when problems is not empty), and records them in the annotate stage's outputs.performance."""
        try:
            c = P.load(pipeline)
            o = P.stage(c, "annotate")["outputs"] or {}
            spec = o.get("spec")
            if not spec:
                return {"status": "error", "error": "no spec: call annotate_spec first"}
            dst = P.work(c) / "source"
            if not dst.is_dir():
                return {"status": "error", "error": "annotate_apply has not run: there is no provenance copy"}
            entries = _entries_of(spec)
            before = set((o.get("performance") or {}).get("files") or [])
            by = {}
            probs = []
            for e in entries:
                rel = str(e.get("file", ""))
                if not _safe(rel) or not (dst / rel).is_file():
                    probs.append(f"{rel or '?'}: no such file in the copy")
                elif not rel.endswith(".py"):
                    probs.append(f"{rel}: performance annotation is Python only (C and C++ are a follow-up)")
                else:
                    by.setdefault(rel, []).append(e)
            res = {"files": sorted(by), "applied": [], "skipped": [], "problems": probs}
            diff = []
            for rel in sorted(set(by) | before):
                f = dst / rel
                if not f.is_file():
                    continue
                old = f.read_text()
                r = perf_insert(old, by.get(rel, []), rel)
                res["applied"] += r["applied"]
                res["skipped"] += r["skipped"]
                res["problems"] += r["problems"]
                if r["text"] != old:
                    f.write_text(r["text"])
                    diff += list(difflib.unified_diff(old.splitlines(True), r["text"].splitlines(True), f"a/{rel}", f"b/{rel}", n=1))
            res["diff"] = "".join(diff)[:12000]
            P.record(pipeline, "annotate", outputs={"performance": {"files": res["files"], "applied": res["applied"], "skipped": res["skipped"], "problems": res["problems"]}},
                     note=f"performance: {len(res['applied'])} applied, {len(res['skipped'])} skipped")
            return {"status": "error" if res["problems"] else "ok", **res}
        except (P.PipelineError, ValueError, OSError, KeyError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def perf_validate(pipeline: str) -> dict:
        """Validator of the performance annotation (annotate_validate calls it): every file with entries still parses, dftracer is imported exactly once there, every
        dftracer name used resolves to that import, the begin/end markers of regions balance, the number of inserted decorators and regions equals what perf_apply
        applied, and every spec entry was applied or skipped with a reason. Use after annotate_apply / perf_apply. Returns {status: pass|fail|none, failed, checks:
        [{file, name, ok, detail, fix}], unaccounted, dftracer_importable}; none when the spec has no performance section."""
        try:
            c = P.load(pipeline)
            o = P.stage(c, "annotate")["outputs"] or {}
            entries = _entries_of(o.get("spec"))
            if not entries:
                return {"status": "none", "failed": [], "checks": [], "unaccounted": [], "detail": "the spec has no performance section"}
            perf = o.get("performance") or {}
            dst = P.work(c) / "source"
            rows, n_by = [], {}
            for a in perf.get("applied") or []:
                n_by[a["file"]] = n_by.get(a["file"], 0) + 1
            for rel in sorted({str(e.get("file", "")) for e in entries}):
                f = dst / rel
                if not (_safe(rel) and f.is_file()):
                    rows.append({"file": rel, "name": "file_found", "ok": False, "detail": "not in the provenance copy", "fix": "fix the entry's file; annotate_apply"})
                    continue
                rows += [{"file": rel, "name": n, "ok": ok, "detail": d, "fix": fx} for n, ok, d, fx in _check_text(f.read_text(), n_by.get(rel, 0) if n_by.get(rel) else None)]
            done = {(a["file"], a.get("target"), a.get("anchor")) for a in perf.get("applied") or []} | {(s["file"], s.get("function"), s.get("anchor")) for s in perf.get("skipped") or []}
            un = [e.get("function") or e.get("anchor") for e in entries if (e.get("file"), e.get("function") or e.get("in") or "module", e.get("anchor")) not in done
                  and (e.get("file"), e.get("function"), e.get("anchor")) not in done]
            rows.append({"file": "", "name": "entries_accounted", "ok": not un and not perf.get("problems"), "detail": f"not applied or skipped: {un}; problems: {perf.get('problems')}",
                         "fix": "perf_apply (or annotate_apply) and read its problems"})
            try:
                import importlib.util
                imp = importlib.util.find_spec("dftracer.python") is not None
            except Exception:  # noqa: BLE001
                imp = None
            failed = [r["name"] + (":" + r["file"] if r["file"] else "") for r in rows if not r["ok"]]
            return {"status": "fail" if failed else "pass", "failed": failed, "checks": rows, "unaccounted": un, "dftracer_importable": imp}
        except (P.PipelineError, ValueError, OSError, KeyError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def perf_diagram(pipeline: str) -> dict:
        """The performance-annotated functions and regions of an applied annotation, for the annotate gate and the annotation diagram (annotate_diagram embeds
        it): a Markdown table of file, target, kind, dftracer category and event name, then the skipped entries with their reason. Use after perf_apply.
        Returns {status, markdown, annotated: [{file, target, kind, category, name}], skipped}."""
        try:
            c = P.load(pipeline)
            perf = (P.stage(c, "annotate")["outputs"] or {}).get("performance") or {}
            ap, sk = perf.get("applied") or [], perf.get("skipped") or []
            md = ["### Performance annotation (dftracer calls in the copy)", ""]
            if ap:
                md += ["| file | target | kind | category | event name |", "|---|---|---|---|---|"]
                md += [f"| {a['file']} | {a.get('target')}{(' @ ' + a['anchor']) if a.get('anchor') else ''} | {a['kind']} | {a['category']} | {a['name']} |" for a in ap]
            else:
                md.append("No function or region is performance-annotated.")
            if sk:
                md += ["", "Skipped: " + "; ".join(f"{s.get('function') or s.get('anchor')} ({s['reason']})" for s in sk)]
            return {"status": "ok", "markdown": "\n".join(md), "annotated": ap, "skipped": sk}
        except (P.PipelineError, ValueError, OSError, KeyError) as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    def perf_annotate_selftest() -> dict:
        """Self-check of the performance annotation on an embedded sample app, with no pipeline: the planner proposes the subprocess region and the I/O helper, the
        insertion writes dftracer calls that parse with one import, the trivial hot-loop function is refused by the cost gate, and stripping then re-inserting is
        byte-identical. Run after any change to this capability. Returns {status, missing, line}."""
        miss = []
        plan = _plan({"app.py": SAMPLE}, [{"name": "run_all", "file": "app.py", "function": "Runner.run", "mode": "wrap"}], {})
        got = {(e.get("function") or e.get("anchor"), e["category"]) for e in plan["entries"]}
        for want in (("subprocess.run([\"true\"], check=False)", "subprocess"), ("load", "io")):
            if want not in got:
                miss.append(f"planner entry {want}")
        if not any("tiny" in str(s.get("function")) for s in plan["skipped"]):
            miss.append("planner skip of tiny")
        r = perf_insert(SAMPLE, SAMPLE_ENTRIES, "app.py")
        t = r["text"]
        for want in ('_dft_ai.compute.derive("Runner.run")', '_dft_ai.data.io.derive("load")', 'with _DftFn("subprocess", name="run.wait"):'):
            if want not in t:
                miss.append(f"inserted {want}")
        if len(r["applied"]) != 3 or [s["function"] for s in r["skipped"]] != ["tiny"] or r["problems"]:
            miss.append(f"applied {len(r['applied'])}, skipped {r['skipped']}, problems {r['problems']}")
        if t.count("from dftracer.python import") != 1:
            miss.append("dftracer imported once")
        if _strip(t) != SAMPLE:
            miss.append("strip restores the source")
        if perf_insert(t, SAMPLE_ENTRIES, "app.py")["text"] != t:
            miss.append("idempotent")
        miss += [f"check {n}: {d}" for n, ok, d, _f in _check_text(t, 3) if not ok]
        return {"status": "error" if miss else "ok", "missing": miss, "line": "perf_annotate selftest " + ("FAIL: " + str(miss) if miss else "PASS")}
