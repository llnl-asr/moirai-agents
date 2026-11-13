"""Run pipeline stage 8: the graph health gate. The run is done only when every artifact has a producer and reaches real inputs.

Moved from dftracer-agents' provenance mode, reading moirai's records.
graph_compare compares by sha256 and descends into tar archives member by member (and into .zst members).
"""
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from moirai_agents.core import pipeline as P
from moirai_agents.core import provtrace as T

STAGE = "graph"
ROOT_ROLES = {"input", "parameter", "reference"}
MAX_VOLATILE_LINES = 8
MAX_COMPARE_FILES = 500
SELF_NOTE = " (same file as plain: not compared)"
VOLATILE_DEFAULT = [
    r"^\s*(Date|Command)\b",
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?",
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\d{2}:\d{2}:\d{2}",
    r"/mnt/nnf/[0-9a-fA-F-]{8,}",
]


def _health(g, spec):
    declared = {t["type"]: t for t in spec.get("entity_types", [])}
    artifacts = spec.get("scientific_artifacts", [])
    makers, users = {}, {}
    for e in g.edges:
        (makers if e["kind"] in ("generated", "updated") else users if e["kind"] == "used" else {}).setdefault(e["entity"], []).append(e["activity"])
    inputs = {}
    for e in g.edges:
        if e["kind"] == "used":
            inputs.setdefault(e["activity"], []).append(e["entity"])
    by_type = Counter(e["type"] for e in g.entities.values())
    lineage, unproduced, bad_roots = {}, [], {}
    for eid, e in g.entities.items():
        if e["type"] not in artifacts:
            continue
        if eid not in makers:
            unproduced.append(eid)
        seen, stack, roots = set(), [eid], set()
        while stack:
            cur = stack.pop()
            ms = makers.get(cur, [])
            if not ms and cur != eid:
                roots.add(cur)
            for a in ms:
                for u in inputs.get(a, []):
                    if u not in seen:
                        seen.add(u)
                        stack.append(u)
        types = {g.entities.get(r, {}).get("type", "?") for r in roots}
        lineage[eid] = {"type": e["type"], "uri": e.get("uri", ""), "ancestors": len(seen), "root_types": sorted(types)}
        wrong = sorted(t for t in types if (declared.get(t, {}).get("role") or (g.types.get(t) or {}).get("role")) not in ROOT_ROLES)   # captured inputs: role parameter, from the trace
        if wrong:
            bad_roots[eid] = wrong
    return {"records": g.counts()["records"], "activities": len(g.activities), "entities": len(g.entities), "entities_by_type": dict(by_type), "dangling": len(g.dangling()),
            "artifact_types_missing": sorted(a for a in artifacts if not by_type.get(a)), "artifacts_without_producer": len(unproduced),
            "types_undescribed": sorted(t for t in by_type if not (g.types.get(t) or {}).get("description")),
            "roots_not_inputs": bad_roots, "lineage_example": next(iter(lineage.values()), None), "artifacts": len(lineage)}


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _decompress(data):
    """Return decompressed bytes of zstd data, or None when no decompressor is available or it fails."""
    try:
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
    except ImportError:
        pass
    except Exception:
        return None
    try:
        r = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, timeout=120)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _diff_lines(x, y, patterns, max_lines=MAX_VOLATILE_LINES):
    """Classify two byte strings with different hashes. Returns ('timestamp_only', n_lines) or ('different', n_lines)."""
    xs, ys = x.decode("utf-8", "replace").splitlines(), y.decode("utf-8", "replace").splitlines()
    if len(xs) != len(ys):
        return "different", abs(len(xs) - len(ys))
    rx = [re.compile(p) for p in patterns]
    bad = [(p, q) for p, q in zip(xs, ys) if p != q]
    volatile = all(any(r.search(p) for r in rx) and any(r.search(q) for r in rx) for p, q in bad)
    if bad and volatile and len(bad) <= max_lines:
        return "timestamp_only", len(bad)
    return "different", len(bad)


def _compare_bytes(x, y, name, patterns):
    """Compare two member payloads. Returns (kind, lines): kind in identical|timestamp_only|different."""
    if _sha(x) == _sha(y):
        return "identical", 0
    if name.endswith(".zst"):
        dx, dy = _decompress(x), _decompress(y)
        if dx is None or dy is None:
            return "different", "compressed, compared as bytes"
        if _sha(dx) == _sha(dy):
            return "identical", 0
        x, y = dx, dy
    return _diff_lines(x, y, patterns)


def _members(path):
    out = {}
    with tarfile.open(path) as tf:
        for m in tf:
            if m.isfile():
                out[m.name] = tf.extractfile(m).read()
    return out


def _member_count(path):
    """Number of regular members of a tar, else 1 (a plain file hashed whole counts as one compared unit)."""
    try:
        if tarfile.is_tarfile(path):
            return max(1, len(_members(path)))
    except (OSError, tarfile.TarError):
        pass
    return 1


def compare_files(a, b, rel, patterns=None):
    """Pure comparison of one relative file under dirs a and b. Returns a result dict with identical, timestamp_only, different, missing, compared_members.
    A file that resolves to the same path under a and b is never compared with itself: it is reported under missing with a note and compared_members stays 0."""
    patterns = patterns or VOLATILE_DEFAULT
    res = {"identical": [], "timestamp_only": [], "different": [], "missing": [], "compared_members": 0}
    pa, pb = Path(a) / rel, Path(b) / rel
    if not pb.is_file():
        res["missing"].append(rel)
        return res
    if not pa.is_file():
        return res
    if pa.resolve() == pb.resolve():
        res["missing"].append(rel + SELF_NOTE)
        return res
    is_tar = False
    if _digest(pa) == _digest(pb):
        res["identical"].append(rel)
        res["compared_members"] = _member_count(pa)
        return res
    try:
        is_tar = tarfile.is_tarfile(pa) and tarfile.is_tarfile(pb)
    except OSError:
        is_tar = False
    if not is_tar:
        kind, n = _compare_bytes(pa.read_bytes(), pb.read_bytes(), str(rel), patterns)
        res["compared_members"] = 1
        if kind == "timestamp_only":
            res["timestamp_only"].append({"file": rel, "member": "", "lines": n})
        else:
            res["different"].append(rel)
        return res
    ma, mb = _members(pa), _members(pb)
    bad, ts = [], []
    for name in sorted(set(ma) | set(mb)):
        if name not in mb:
            res["missing"].append(f"{rel}::{name}")
            continue
        if name not in ma:
            bad.append(f"{rel}::{name}")
            continue
        res["compared_members"] += 1
        kind, n = _compare_bytes(ma[name], mb[name], name, patterns)
        if kind == "different":
            bad.append(f"{rel}::{name}")
        elif kind == "timestamp_only":
            ts.append({"file": rel, "member": name, "lines": n})
    res["different"] += bad
    res["timestamp_only"] += ts
    if not bad and not ts and not res["missing"]:
        res["identical"].append(rel)
    return res


def compare_tree(a, b, rels, patterns=None):
    """Pure: compare each relative path in rels. Returns {identical, timestamp_only, different, missing, compared_members}."""
    out = {"identical": [], "timestamp_only": [], "different": [], "missing": [], "compared_members": 0}
    for rel in rels:
        r = compare_files(a, b, rel, patterns)
        for k in out:
            out[k] = out[k] + r[k]
    return out


def default_files(plain, limit=2000):
    """Pure: files worth comparing under a plain study dir: AlphaFold outputs-af3 trees and *_cut50.pdb. Rosetta outputs are unseeded, so never defaulted."""
    root = Path(plain)
    if not root.is_dir():
        return []
    rels = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file() and ("outputs-af3" in p.parts or p.name.endswith("_cut50.pdb"))]
    return rels[:limit]


def all_files(plain, limit=MAX_COMPARE_FILES):
    """Pure: every regular file under plain, relative posix paths, sorted. Returns (rels[:limit], capped)."""
    root = Path(plain)
    if not root.is_dir():
        return [], False
    rels = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()]
    return rels[:limit], len(rels) > limit


def relativize_outputs(plain, outs):
    """Pure: outputs_created entries as paths relative to plain. Absolute entries under plain are made relative; absolute entries elsewhere are ignored with a note.
    Returns (rels, notes)."""
    root, rels, notes = Path(plain), [], []
    for o in outs or []:
        p = Path(str(o))
        if not p.is_absolute():
            rels.append(p.as_posix())
            continue
        try:
            rels.append(p.relative_to(root).as_posix())
        except ValueError:
            try:
                rels.append(p.resolve().relative_to(root.resolve()).as_posix())
            except ValueError:
                notes.append(f"ignored absolute output outside plain_dir: {o}")
    return rels, notes


def science_status(o):
    """Pure: map recorded compare outputs to (state, message). state in pass|fail|inconclusive."""
    if "compare" not in o:
        return "inconclusive", "graph_compare has not run"
    if not o.get("compare_files"):
        return "inconclusive", "graph_compare compared no file (empty, missing or self-compared list): pass is vacuous, give plain_dir, traced_dir of two different runs and files"
    if o.get("compare_differs"):
        return "fail", f"outputs differ in content from the plain run: {o['compare_differs'][:5]}"
    if o.get("compare_missing"):
        return "inconclusive", f"files not compared (missing in the traced run or the same file as plain): {o['compare_missing'][:5]}"
    cmp_ = o.get("compare") or {}
    if "compared_members" in cmp_ and not cmp_["compared_members"]:
        return "inconclusive", "compared_members is 0: nothing was really compared (same file on both sides?); compare plain against traced outputs of two different runs"
    if o.get("compare_timestamp_only"):
        names = [f"{t['file']}::{t['member']}" if t.get("member") else t["file"] for t in o["compare_timestamp_only"]][:10]
        return "inconclusive", f"only timestamp/command-line differences (user's call): {names}"
    return "pass", ""


def _selftest():
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a", Path(td) / "b"
        a.mkdir(), b.mkdir()

        def mk(path, members):
            with tarfile.open(path, "w") as tf:
                for n, d in members.items():
                    ti = tarfile.TarInfo(n)
                    ti.size = len(d)
                    tf.addfile(ti, io.BytesIO(d))
        same = b"hit 1\nhit 2\n"
        mk(a / "t1.tar", {"0/x.txt": same})
        mk(b / "t1.tar", {"0/x.txt": same})
        mk(a / "t2.tar", {"0/x.txt": same, "0/h.hhr": b"Date:   Fri Oct 30 10:00:00 2026\nCommand: hhsearch /mnt/nnf/1234abcd-0000/in\nscore 5\n"})
        mk(b / "t2.tar", {"0/x.txt": same, "0/h.hhr": b"Date:   Sat Oct 31 11:11:11 2026\nCommand: hhsearch /mnt/nnf/9999ffff-1111/in\nscore 5\n"})
        mk(a / "t3.tar", {"0/x.txt": b"score 5\n"})
        mk(b / "t3.tar", {"0/x.txt": b"score 6\n"})
        res = compare_tree(a, b, ["t1.tar", "t2.tar", "t3.tar", "t4.tar"])
        # the defect: absolute outputs_created of the plain run, joined under both dirs, resolve to the same plain file
        abs_outs = [str(a / "t1.tar"), str(a / "t2.tar")]
        res["self_compare"] = compare_tree(a, a, ["t1.tar"])
        res["self_compare_abs_join"] = compare_tree(a, b, abs_outs)
        rels, notes = relativize_outputs(a, abs_outs + ["/elsewhere/x.tar", "t3.tar"])
        res["resolved"] = {"from_outputs": rels, "notes": notes}
        res["files_argument"] = compare_tree(a, b, ["t1.tar", "t3.tar"])
        res["walk"] = all_files(a)[0]
        return res


def register(mcp):
    @mcp.tool()
    def graph_check(pipeline: str) -> dict:
        """Build the provenance graph of the baseline run and measure its health against the spec, and write <pipeline>/graph.json. Use after the baseline.
        Returns {activities, entities, entities_by_type, dangling, artifact_types_missing, artifacts_without_producer, types_undescribed, roots_not_inputs, lineage_example}."""
        c = P.load(pipeline)
        traces = P.stage(c, "baseline")["outputs"].get("traces")
        if not traces:
            return {"status": "error", "error": "the baseline stage has no traces"}
        spec = P.stage(c, "annotate")["outputs"].get("spec") or {}
        g = T.read(traces)
        h = _health(g, spec)
        (P.work(c) / "graph.json").write_text(json.dumps({**g.view(10**9), "health": h}, default=str))
        P.record(pipeline, STAGE, status="running", outputs={"health": h, "summary": f"{h['artifacts']} artifact(s), {h['activities']} activities, {h['dangling']} dangling"}, note="graph built")
        return h

    @mcp.tool()
    def graph_compare(pipeline: str, plain_dir: str = "", traced_dir: str = "", volatile_patterns: list = None, files: list = None) -> dict:
        """Check the run did not change the science: compare the files the plain run wrote with the same files from the traced run, by sha256 of content.
        Tar archives are opened and compared member by member; .zst members are decompressed (zstandard module, else the zstd CLI, else compared as bytes).
        A member whose only differing lines match volatile_patterns (default: Date/Command header lines, timestamps, /mnt/nnf/<uuid> paths; at most 8 lines) is timestamp_only, not different.
        Use after graph_check. Compare plain against traced outputs of two DIFFERENT runs (e.g. runs/smoke/output vs runs/moirai-baseline-N/output); a path never gets compared with itself.
        plain_dir: default runs/baseline_plain/study when it and runs/baseline/study exist, else where smoke ran. traced_dir: default runs/baseline/study, else the annotated tree.
        files: optional list of paths relative to plain_dir/traced_dir (tars are opened); used before any default. Without files: when plain_dir and traced_dir are both given and exist, every regular file under plain_dir (cap 500, reported in notes);
        else the AlphaFold outputs-af3 tree and *_cut50.pdb of a study dir; else smoke's outputs_created (absolute entries under plain_dir are made relative, others ignored with a note).
        A file resolving to the same path on both sides is reported under missing and not compared. Zero files or compared_members 0 make science_unchanged inconclusive, never a pass.
        volatile_patterns: optional list of regexes replacing the default.
        Returns {identical, timestamp_only: [{file, member, lines}], different, missing, compared_members, notes}; timestamp_only is for the user to judge."""
        c = P.load(pipeline)
        smoke = P.stage(c, "smoke")["outputs"]
        runs = P.work(c) / "runs"
        explicit = bool(plain_dir and traced_dir)
        if not plain_dir and (runs / "baseline_plain" / "study").is_dir() and (runs / "baseline" / "study").is_dir():
            plain_dir, traced_dir = str(runs / "baseline_plain" / "study"), traced_dir or str(runs / "baseline" / "study")
        a, b = Path(plain_dir or smoke.get("cwd") or P.source_of(c)), Path(traced_dir or P.stage(c, "integrate")["outputs"].get("annotated", ""))
        notes = []
        if files:
            rels = [str(f) for f in files]
        else:
            rels = default_files(a) if a.name == "study" else []
            if not rels and explicit and a.is_dir() and b.is_dir():
                rels, capped = all_files(a)
                if capped:
                    notes.append(f"capped at {MAX_COMPARE_FILES} files; pass files= to compare the rest")
            if not rels:
                rels, n2 = relativize_outputs(a, smoke.get("outputs_created", []))
                notes += n2
        if a.exists() and b.exists() and a.resolve() == b.resolve():
            notes.append("plain_dir and traced_dir are the same directory: nothing can be compared; pass the outputs of two different runs")
        out = compare_tree(a, b, rels, volatile_patterns)
        nfiles = len(out["identical"]) + len(out["timestamp_only"]) + len(out["different"])
        P.record(pipeline, STAGE, outputs={"compare_files": nfiles, "compare": {k: (len(v) if isinstance(v, list) else v) for k, v in out.items()}, "compare_differs": out["different"][:20],
                                           "compare_missing": out["missing"][:20], "compare_timestamp_only": out["timestamp_only"][:20]}, note="outputs compared")
        out["notes"] = notes
        return out

    @mcp.tool()
    def graph_compare_dirs(plain_dir: str, traced_dir: str, files: list, volatile_patterns: list = None) -> dict:
        """Same comparison as graph_compare but on two directories and a list of relative file paths, with no pipeline and nothing recorded. Use to re-check two runs by hand.
        plain_dir, traced_dir: roots; files: relative paths (tars are opened); volatile_patterns: optional regexes. A file that is the same path on both sides is reported under missing.
        Returns {identical, timestamp_only, different, missing, compared_members}."""
        return compare_tree(plain_dir, traced_dir, files, volatile_patterns)

    @mcp.tool()
    def graph_compare_selftest() -> dict:
        """Build two small tar sets in a temp dir (identical, Date/Command-header-only difference, real difference, missing), the self-compare defect (absolute paths, same dir) and the files argument, and compare them. Use to confirm graph_compare logic.
        Returns the comparison plus science_state for each case."""
        r = _selftest()
        r["states"] = {
            "identical": science_status({"compare": {}, "compare_files": 1})[0],
            "timestamp_only": science_status({"compare": {}, "compare_files": 1, "compare_timestamp_only": r["timestamp_only"]})[0],
            "different": science_status({"compare": {}, "compare_files": 1, "compare_differs": r["different"]})[0],
            "nothing_compared": science_status({"compare": {}})[0],
            "self_compare": science_status({"compare": {"compared_members": 0}, "compare_files": 0, "compare_missing": r["self_compare"]["missing"]})[0],
            "zero_members": science_status({"compare": {"compared_members": 0}, "compare_files": 1})[0],
        }
        return r

    @mcp.tool()
    def graph_validate(pipeline: str) -> dict:
        """The health gate: provenance was recorded, no dangling id, every artifact type present with a producer, every root a real input, every type described. Use before the gate.
        science_unchanged: pass when outputs are identical, inconclusive when only timestamp differences exist (named), nothing was compared, files were missing or self-compared, or compared_members is 0; fail on real content differences. Returns the verdict {status, failed, unknown, passed, total}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        h = o.get("health")
        if v.check("built", bool(h), "graph_check has not run", "run graph_check"):
            v.check("recorded", h["activities"] > 0, "no activity in the trace", "did the annotated code run?")
            v.check("no_dangling", h["dangling"] == 0, f"{h['dangling']} ids with no EH record", "a process's trace is missing, or an entity is related but never declared")
            v.check("artifacts_present", not h["artifact_types_missing"], f"missing: {h['artifact_types_missing']}", "annotation gap at the writer, or the run did not reach it")
            v.check("artifacts_produced", h["artifacts_without_producer"] == 0, f"{h['artifacts_without_producer']} without a producer", "add generated at the writer: every artifact needs a producer activity")
            v.check("roots_are_inputs", not h["roots_not_inputs"], str(h["roots_not_inputs"])[:300], "a producer upstream is not annotated: walk back and add it")
            v.check("types_described", not h["types_undescribed"], f"undescribed: {h['types_undescribed']}", "declare the type with its description in the spec, and annotate again")
            state, msg = science_status(o)
            if state == "fail":
                v.check("science_unchanged", False, msg, "a real content difference: find the member and the cause (tracing changed behaviour?) before accepting")
            elif state == "inconclusive":
                v.inconclusive("science_unchanged", msg)
            else:
                v.check("science_unchanged", True, "", "")
        return P.finish_validation(pipeline, v)
