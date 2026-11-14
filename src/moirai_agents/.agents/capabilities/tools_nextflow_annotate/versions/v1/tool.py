"""tools_nextflow_annotate: annotate a COPY of a Nextflow pipeline so each process task reports its activity and entities to moirai through a bash EXIT trap that calls a generated helper; spec, diagram, apply, validate, preview and selftest. Nothing here writes outside the pipeline's provenance copy."""
import ast
import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from moirai_agents.core import pipeline as P

STAGE = "annotate"
OPEN, CLOSE = "# >>> moirai-agents nextflow_annotate", "# <<< moirai-agents nextflow_annotate"
SKIP = shutil.ignore_patterns(".git", "__pycache__", ".nextflow", "work", "*.swp")
SKIP_DIRS = {".git", "__pycache__", ".nextflow", "work"}
PRIV = re.compile(r"/(?:" + "|".join(["ho" + "me", "Us" + "ers", "us" + "r/WS", "p/lu" + "stre", "g/" + "g\\d"]) + ")")
STAGES = {"g_prep": "Stage: reads, trimming, human filtering, QC", "g_class": "Stage: classification (kaiju, centrifuge, centrifuger, esviritu, seqscreen)", "g_rep": "Stage: recentrifuge reports"}

# ---- the entity plan (agreed in discovery.md and the approved diagram) ------------------------------------------------------------------
_T = [("raw_reads", "input", "raw reads (paired)", "Paired raw FASTQ reads of a sample from the samplesheet."),
      ("trimmed_reads", "intermediate", "fastp-trimmed reads", "Reads after fastp."),
      ("fastp_report", "output", "fastp json/html", "fastp quality report; attributes carry read counts before and after filtering."),
      ("reads_minimap_a", "intermediate", "reads after MINIMAP_A (humanreffilt a)", "Reads left after minimap2 against the first human reference (CHM13)."),
      ("human_filtered_reads", "intermediate", "human-filtered reads (after MINIMAP_B)", "Reads left after both human reference filters (CHM13 then GRCh38)."),
      ("seqkit_stats", "output", "seqkit stats (all1.tsv)", "seqkit statistics of the human-filtered reads; attributes carry the counts."),
      ("fasta_reads", "intermediate", "fasta per mate", "Human-filtered reads converted to FASTA for SeqScreen."),
      ("seqscreen_output", "output", "seqscreen run directory + taxonomic_results, functional_results, report zip, threats_*", "One SeqScreen run directory and its main report files."),
      ("esviritu_tables", "output", "esviritu tables (detected_virus.info, assembly_summary, tax_profile)", "EsViritu result tables."),
      ("centrifuge_results", "intermediate", "centrifuge results", "Centrifuge report and results files."),
      ("centrifuger_results", "intermediate", "centrifuger results", "Centrifuger results table."),
      ("kaiju_results", "intermediate", "kaiju.tsv", "Kaiju classification of the reads."),
      ("kaiju_names", "output", "kaiju names.tsv", "Kaiju result with taxon names."), ("kaiju_counts", "output", "kaiju counts.tsv", "Kaiju read counts per taxon."),
      ("kaiju_summary", "output", "kaiju summary.tsv", "Kaiju summary table."), ("kaiju_recentrifuge_input", "intermediate", "kaiju2recentrifuge.tsv", "Kaiju result in recentrifuge format."),
      ("kaijux_results", "output", "kaijux.tsv", "kaijux results and their match-id subset."),
      ("recentrifuge_report", "output", "recentrifuge reports (rcf.html, data.tsv, stat.tsv)", "Recentrifuge report of one classifier."),
      ("reference_database", "reference", "database", "A reference database: attributes path, size, mtime; never hashed."),
      ("software", "reference", "software", "A tool and its version (versions.yml) or container image.")]
TYPES = [{"name": n, "role": r, "label": lb, "description": d} for n, r, lb, d in _T]


def _u(t, expr, kind="file", **kw):
    return {"type": t, "expr": expr, "kind": kind, **kw}


def _db(name, expr, **kw):
    return {"type": "reference_database", "expr": expr, "kind": "db", "name": name, **kw}


def _g(glob, t, kind="file", **kw):
    return {"glob": glob, "type": t, "kind": kind, **kw}


def _r(rel, subject, obj, label="", **kw):
    return {"rel": rel, "subject": subject, "object": obj, "label": label or rel, **kw}


def _cls(subject, db, hf="human_filtered_reads", **kw):
    return [_r("derived_from", subject, hf, "classification of", **kw), _r("depends_on", subject, "reference_database", "classified against", object_name=db, **kw)]


def _p(file, stage, short, key, used, generated, relations, image="", **kw):
    return {"file": file, "stage": stage, "short": short, "key": key, "image": image, "used": used, "generated": generated, "relations": relations, "ext": kw.pop("ext", ["args"]), **kw}


_HF, _K, _KD = "human_filtered_reads", "kaiju_results", "modules/local/custom_kaiju/main.nf"
CATALOG = {
    "FASTP": _p("modules/nf-core/fastp/main.nf", "g_prep", "fastp", "meta.id", [_u("raw_reads", "reads")],
                [_g("*.fastp.fastq.gz", "trimmed_reads"), _g("*.json", "fastp_report", parse="fastp_json"), _g("*.html", "fastp_report")],
                [_r("derived_from", "trimmed_reads", "raw_reads"), _r("derived_from", "fastp_report", "raw_reads", "quality report of")], "fastp"),
    "MINIMAP": _p("modules/local/minimap/main.nf", "g_prep", "minimap", "meta.id",
                  [_u("trimmed_reads", "reads", only=["MINIMAP_A"]), _u("reads_minimap_a", "reads", only=["MINIMAP_B"]),
                   _db("minimap_db_a", "db", only=["MINIMAP_A"]), _db("minimap_db_b", "db", only=["MINIMAP_B"])],
                  [_g("*.fastq.gz", "reads_minimap_a", only=["MINIMAP_A"]), _g("*.fastq.gz", _HF, only=["MINIMAP_B"])],
                  [_r("derived_from", "reads_minimap_a", "trimmed_reads", only=["MINIMAP_A"]), _r("depends_on", "reads_minimap_a", "reference_database", "filtered against", object_name="minimap_db_a", only=["MINIMAP_A"]),
                   _r("derived_from", _HF, "reads_minimap_a", only=["MINIMAP_B"]), _r("depends_on", _HF, "reference_database", "filtered against", object_name="minimap_db_b", only=["MINIMAP_B"])],
                  "minimap2 + samtools", ext=["args", "args2", "args3", "args4"],
                  aliases={"MINIMAP_A": {"short": "ma", "info": "minimap2 | samtools view -f 12 -F 256 | samtools fastq, CHM13."}, "MINIMAP_B": {"short": "mb", "info": "Same pipeline with GRCh38."}}),
    "SEQKIT_STATS": _p("modules/nf-core/seqkit/stats/main.nf", "g_prep", "seqkit", "meta.id", [_u(_HF, "reads")], [_g("*.tsv", "seqkit_stats", parse="seqkit_tsv")],
                       [_r("derived_from", "seqkit_stats", _HF, "statistics of")], "seqkit", info="Keyed by the aggregate all1."),
    "FASTQ_TO_FASTA": _p("modules/local/seqscreen/fastq_to_fasta.nf", "g_class", "f2a", "sampleId", [_u(_HF, "fastq")], [_g("*.fasta", "fasta_reads")],
                         [_r("derived_from", "fasta_reads", _HF, "converted from")], ext=[], info="Keyed by mate file name."),
    "SEQSCREEN": _p("modules/local/seqscreen/main.nf", "g_class", "ss", "runName",
                    [_u("fasta_reads", "fasta"), _db("seqscreen_db_root", "db_root"), _db("seqscreen_db_path", "db_path", virtual=True)],
                    [_g("{key}", "seqscreen_output", "dir", contains=["seqscreen_output"])] + [_g("{key}/**/" + x, "seqscreen_output") for x in ("taxonomic_results.txt", "functional_results.txt", "*.zip", "threats_*.txt")],
                    [_r("derived_from", "seqscreen_output", "fasta_reads"), _r("depends_on", "seqscreen_output", "reference_database", "classified against", object_name="seqscreen_db_root")],
                    "seqscreen (podman)", ext=[], info="Run in podman; db root bind-mounted, not staged."),
    "ESVIRITU": _p("modules/local/esviritu/main.nf", "g_class", "ev", "meta.id", [_u(_HF, "reads"), _db("esviritu_db", "db")],
                   [_g("*.detected_virus.info.tsv", "esviritu_tables"), _g("*.detected_virus.assembly_summary.tsv", "esviritu_tables"), _g("*.tax_profile.tsv", "esviritu_tables")],
                   _cls("esviritu_tables", "esviritu_db")[:1] + [_r("depends_on", "esviritu_tables", "reference_database", "classified against", object_name="esviritu_db")], "esviritu", record_on_failure=True),
    "CENTRIFUGE_CENTRIFUGE": _p("modules/nf-core/centrifuge/centrifuge/main.nf", "g_class", "cf", "meta.id", [_u(_HF, "reads"), _db("centrifuge_db", "db")],
                                [_g("*report.txt", "centrifuge_results"), _g("*results.txt", "centrifuge_results")], _cls("centrifuge_results", "centrifuge_db"), "centrifuge"),
    "CENTRIFUGER_CENTRIFUGER": _p("modules/local/centrifuger/main.nf", "g_class", "cg", "meta.id", [_u(_HF, "reads"), _db("centrifuger_db", "db")],
                                  [_g("*results.tsv", "centrifuger_results")], _cls("centrifuger_results", "centrifuger_db"), "centrifuger"),
    "KAIJU_KAIJU": _p(_KD, "g_class", "k", "meta.id", [_u(_HF, "reads"), _db("kaiju_db", "db")], [_g("*.kaiju.tsv", _K)], _cls(_K, "kaiju_db"), "kaiju"),
    "KAIJU_ADDTAXONNAMES": _p(_KD, "g_class", "kn", "meta.id", [_u(_K, "kaiju"), _db("kaiju_db", "db")], [_g("*.names.tsv", "kaiju_names")], [_r("derived_from", "kaiju_names", _K)]),
    "KAIJU_COUNTTAXONREADS": _p(_KD, "g_class", "kc", "meta.id", [_u(_K, "kaiju")], [_g("*.counts.tsv", "kaiju_counts")], [_r("derived_from", "kaiju_counts", _K)]),
    "KAIJU_KAIJU2TABLE": _p(_KD, "g_class", "kt", "meta.id", [_u(_K, "kaiju")], [_g("*.summary.tsv", "kaiju_summary")], [_r("derived_from", "kaiju_summary", _K)]),
    "KAIJU_RECENTRIFUGEFORMAT": _p(_KD, "g_class", "kr", "meta.id", [_u(_K, "kaiju")], [_g("*.kaiju2recentrifuge.tsv", "kaiju_recentrifuge_input")], [_r("derived_from", "kaiju_recentrifuge_input", _K)]),
    "KAIJU_KAIJUX": _p(_KD, "g_class", "kx", "meta.id", [_u(_HF, "reads"), _db("kaijux_db", "db"), _db("kaijux_match_id", "match_id")], [_g("*kaijux.tsv", "kaijux_results")], _cls("kaijux_results", "kaijux_db"), "kaiju"),
    "RECENTRIFUGE": _p("modules/local/recentrifuge/main.nf", "g_rep", "rc", "meta.id + '.' + input_type_str",
                       [_u("kaiju_recentrifuge_input", "results", suffix=".kaiju"), _u("centrifuge_results", "results", suffix=".centrifuge"), _u("centrifuger_results", "results", suffix=".centrifuger"),
                        _db("kaiju_db", "db", suffix=".kaiju", label="taxonomy"), _db("centrifuge_db", "db", suffix=".centrifuge", label="taxonomy"), _db("centrifuger_db", "db", suffix=".centrifuger", label="taxonomy")],
                       [_g("*.out.rcf.html", "recentrifuge_report"), _g("*.out.rcf.data.tsv", "recentrifuge_report"), _g("*.out.rcf.stat.tsv", "recentrifuge_report")],
                       [_r("derived_from", "recentrifuge_report", t, "report of", suffix=s) for t, s in (("kaiju_recentrifuge_input", ".kaiju"), ("centrifuge_results", ".centrifuge"), ("centrifuger_results", ".centrifuger"))],
                       "recentrifuge", record_on_failure=True, label="RECENTRIFUGE (3 tasks per sample: kaiju, centrifuge, centrifuger)", info="5 rcf calls per task; later calls may leave outputs missing (errorStrategy ignore)."),
}
EXCLUDED = ["SRAHUMANSCRUBBER", "GUNZIP", "CENTRIFUGE_KREPORT", "KRAKENTOOLS_KREPORT2KRONA", "KRONA_KTIMPORTTEXT", "SEQTK_MERGEPE"]


def _build_spec(src, discovery="", exclude=(), overrides=None):
    """Pure: the spec for the processes of `src` that the catalog knows, plus notes (excluded, missing, unknown, not_in_discovery)."""
    present = {}
    for f in Path(src).rglob("*.nf"):
        if SKIP_DIRS & set(f.relative_to(src).parts):
            continue
        for m in re.finditer(r"^process\s+(\w+)", f.read_text(errors="replace"), re.M):
            present[m.group(1)] = str(f.relative_to(src))
    procs, notes = {}, {"excluded": [], "missing": [], "unknown": [], "not_in_discovery": []}
    disc = [d.rstrip("/") for d in re.findall(r"modules/[\w./-]+", discovery)]
    for name, e in CATALOG.items():
        base = os.path.dirname(e["file"])
        if name in exclude:
            notes["excluded"].append(name)
        elif present.get(name) != e["file"]:
            notes["missing"].append(name)
        else:
            procs[name] = json.loads(json.dumps(e))
            if disc and not any(d.startswith(base) or base.startswith(d) for d in disc):
                notes["not_in_discovery"].append(name)
    notes["unknown"] = sorted(n for n in present if n not in CATALOG and n not in EXCLUDED)
    for name, patch in (overrides or {}).items():
        if patch is None:
            procs.pop(name, None)
        elif name in procs:
            procs[name].update(patch)
    return {"version": 1, "pipeline": "taxteller", "types": TYPES, "processes": procs}, notes


# ---- Groovy-safe hook text ----------------------------------------------------------------------------------------------------------------
def _esc(s):
    return s.replace("\\", "\\\\").replace("$", "\\$")


def _t(tmpl):
    """Bash template to Groovy GString text: {{expr}} is interpolated, {{b64:expr}} as base64 of its text, everything else has $ and backslash escaped."""
    out, pos = [], 0
    for m in re.finditer(r"\{\{(b64:)?(.*?)\}\}", tmpl):
        out.append(_esc(tmpl[pos:m.start()]))
        out.append("${(" + m.group(2) + ").toString().bytes.encodeBase64().toString()}" if m.group(1) else "${" + m.group(2) + "}")
        pos = m.end()
    return "".join(out) + _esc(tmpl[pos:])


def _hook(proc, e):
    ext = "".join(' --ext %s="{{b64:task.ext.%s ?: \'\'}}"' % (n, n) for n in e.get("ext", []))
    used = "".join(' --used %d="{{b64:%s}}"' % (i, u["expr"]) for i, u in enumerate(e.get("used", [])))
    call = ('      $_t env -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD "$_py" "$_hp" nf-task %s "{{task.process}}" "$_rc" --key "{{b64:%s}}" --cpus "{{task.cpus}}" '
            '--container "{{task.container}}" --root "{{workflow.workDir}}"%s%s' % (proc, e["key"], ext, used))
    L = [f"{OPEN} {proc} (generated by nextflow_annotate_apply; do not edit)", "_moirai_end() {", "  (", "    set +e +u +C", '    _rc="$1"', '    _py="${MOIRAI_PYTHON:-}"',
         '    if [ -z "$_py" ] && [ -n "${MOIRAI_SERVICE:-}" ]; then _py="${MOIRAI_SERVICE%/*}/python"; fi', '    _hp="${MOIRAI_NF_HELPER:-}"',
         '    if [ -n "$_py" ] && [ -x "$_py" ] && [ -f "$_hp" ]; then', '      _t=""; if command -v timeout >/dev/null 2>&1; then _t="timeout 120"; fi', call, "    fi",
         '  ) >/dev/null 2>>"${MOIRAI_NF_LOG:-/dev/null}"', "  return 0", "}", "trap '_moirai_end \"$?\"' EXIT", f"{CLOSE} {proc}"]
    return [_t(x) for x in L]


def _ev(expr, values):
    m = re.fullmatch(r"\((.*)\)\.toString\(\)\.bytes\.encodeBase64\(\)\.toString\(\)", expr, re.S)
    inner = m.group(1) if m else expr
    if values is None:
        return "eA==" if m else "x"
    v = values.get(inner, "" if "task.ext." in inner else None)
    if v is None:
        raise KeyError(inner)
    return base64.b64encode(str(v).encode()).decode() if m else str(v)


def _render(text, values=None):
    """What Groovy would hand to bash: (bash text, problems, missing expressions). values None: every expression becomes a placeholder."""
    out, probs, missing, i = [], [], [], 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            n = text[i + 1:i + 2]
            out.append(n) if n in ("$", "\\") else probs.append("bad escape: backslash before %r" % n)
            i += 2
        elif c == "$":
            if text[i + 1:i + 2] != "{":
                probs.append("bare $ (Groovy interpolates or fails)")
                i += 1
                continue
            d, j = 1, i + 2
            while j < len(text) and d:
                d += (text[j] == "{") - (text[j] == "}")
                j += 1
            if d:
                probs.append("unbalanced ${")
                break
            try:
                out.append(_ev(text[i + 2:j - 1], values))
            except KeyError as k:
                missing.append(str(k.args[0]))
            i = j
        else:
            if text.startswith('"""', i):
                probs.append("triple quote inside a block")
            out.append(c)
            i += 1
    return "".join(out), probs, missing


def _lint(block):
    bash, probs, _m = _render(block)
    code = "\n".join(l for l in bash.split("\n") if not l.lstrip().startswith("#"))
    if re.search(r"(^|[;&|(\s])exit(\s|;|$)", code):
        probs.append("the hook calls exit")
    try:
        r = subprocess.run(["bash", "-n"], input=bash, capture_output=True, text=True, timeout=20)
        if r.returncode:
            probs.append("bash -n: " + r.stderr.strip()[:200])
    except (OSError, subprocess.SubprocessError):
        probs.append("bash unavailable")
    return probs


# ---- placing blocks ------------------------------------------------------------------------------------------------------------------------
def _strip(text):
    out, skip = [], False
    for l in text.split("\n"):
        if not skip and l.lstrip().startswith(OPEN):
            skip = True
        if not skip:
            out.append(l)
        elif l.lstrip().startswith(CLOSE):
            skip = False
    return "\n".join(out)


def _find(lines, name):
    s = next((i for i, l in enumerate(lines) if re.match(r"^process\s+" + re.escape(name) + r"\s*\{", l)), None)
    if s is None:
        return None, None
    return s, next((i for i in range(s + 1, len(lines)) if re.match(r"^process\s+\w+", lines[i])), len(lines))


def _opener(lines, s, t):
    """(index of the lone triple quote that opens the script body, index of stub: or the end) or (None, reason)."""
    sc = next((i for i in range(s, t) if re.match(r"^\s*script:\s*(//.*)?$", lines[i])), None)
    if sc is None:
        return None, "no script: section (shell: and exec: processes are not annotated)"
    stub = next((i for i in range(sc + 1, t) if re.match(r"^\s*stub:\s*$", lines[i])), t)
    prev, i = "", sc + 1
    while i < stub:
        x = lines[i].strip()
        if x == '"""':
            if prev and prev[-1] in "=?:(,+&|":
                i = next((j for j in range(i + 1, stub) if lines[j].strip() == '"""'), stub) + 1
                prev = '"""'
                continue
            return i, stub
        if x and not x.startswith("//"):
            prev = x
        i += 1
    return None, "no lone triple-quote line opens the script"


def _roots(expr):
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "", expr)
    return set(re.findall(r"(?<![\w.])[A-Za-z_]\w*", s)) - {"true", "false", "null", "it"}


def _avail(lines, s, op):
    names = {"task", "workflow", "params"}
    sec, on = [], False
    for l in lines[s:op]:
        if re.match(r"^\s*input:", l):
            on = True
            l = l.split("input:", 1)[1]
        elif on and re.match(r"^\s*(output|when|script|exec|shell|stub):", l):
            on = False
        if on:
            sec.append(re.sub(r"//.*", "", l))
        m = re.match(r"^\s*(?:def\s+)?([A-Za-z_]\w*)\s*=[^=]", l)
        if m:
            names.add(m.group(1))
    for l in sec:
        names |= set(re.findall(r"\b(?:path|val|env|file|each)\s*\(?\s*([A-Za-z_]\w*)", l))
    return names


def _apply_text(text, entries):
    """(annotated text, problems): all old blocks are stripped, one new block is placed for each process of `entries`."""
    lines, problems, ins = _strip(text).split("\n"), [], []
    for proc, e in entries.items():
        s, t = _find(lines, proc)
        if s is None:
            problems.append(f"{proc}: no `process {proc} {{` in {e['file']}")
            continue
        op, stub = _opener(lines, s, t)
        if op is None:
            problems.append(f"{proc}: anchor: {stub}")
            continue
        if re.search(r"\btrap\b[^\n]*\bEXIT\b|^\s*exec\s", "\n".join(lines[op + 1:stub]), re.M):
            problems.append(f"{proc}: the script has its own trap ... EXIT or an exec line")
            continue
        av = _avail(lines, s, op)
        exprs = [e["key"]] + [u["expr"] for u in e.get("used", [])]
        bad = sorted(r for x in exprs for r in _roots(x) if r not in av)
        if bad:
            problems.append(f"{proc}: names_resolve: {bad} are not inputs or script variables")
            continue
        nxt = next((l for l in lines[op + 1:stub] if l.strip()), "    ")
        ins.append((op, [nxt[:len(nxt) - len(nxt.lstrip())] + l for l in _hook(proc, e)]))
    for op, blk in sorted(ins, reverse=True):
        lines[op + 1:op + 1] = blk
    return "\n".join(lines), problems


def _blocks(text):
    out, cur, name = {}, None, ""
    for l in text.split("\n"):
        if cur is None and l.lstrip().startswith(OPEN):
            name, cur = l.lstrip()[len(OPEN):].split()[0], []
        if cur is not None:
            cur.append(l.strip())
            if l.lstrip().startswith(CLOSE):
                out.setdefault(name, []).append("\n".join(cur))
                cur = None
    return out


# ---- helper (runs in the moirai venv, called by the trap) ---------------------------------------------------------------------------------
HELPER_SRC = r'''"""Generated by moirai-agents nextflow_annotate_apply: reports one Nextflow task to moirai. Regenerated on every apply; do not edit.
`python _moirai_nf.py nf-task MODULE_PROCESS TASK_PROCESS EXIT_CODE --key B64 ...` is called by the EXIT trap of an annotated process; `nf-plan` prints what
would be recorded and needs no moirai. It never raises and always returns 0."""
import argparse
import base64
import glob
import hashlib
import json
import os
import socket
import sys
import time

SPEC = json.loads(base64.b64decode("__SPEC__").decode())
MAX_WALK, MAX_VALUES = 5000, 48


def _b64(s):
    try:
        t = base64.b64decode(s.encode()).decode("utf-8", "replace")
    except Exception:
        return ""
    return "" if t == "null" else t


def _ok(item, alias, key):
    return (not item.get("only") or alias in item["only"]) and (not item.get("suffix") or key.endswith(item["suffix"]))


def _measure(real):
    st = os.stat(real)
    out = {"size": st.st_size, "mtime": int(st.st_mtime)}
    if os.path.isdir(real):
        total = n = 0
        for dp, _d, fn in os.walk(real):
            for f in fn:
                n += 1
                if n <= MAX_WALK:
                    try:
                        total += os.lstat(os.path.join(dp, f)).st_size
                    except OSError:
                        pass
            if n > MAX_WALK:
                break
        out.update({"size": total, "entries": min(n, MAX_WALK), "size_partial": str(n > MAX_WALK).lower()})
    return out


def _under(real, root):
    r = os.path.realpath(root) if root else ""
    return os.path.relpath(real, r) if r and (real == r or real.startswith(r + os.sep)) else None


def _fkey(real, root, size):
    rel = _under(real, root)
    return rel if rel is not None else "ext/%s@%d" % (os.path.basename(real), size)


def _ent(t, key, real, attrs):
    return {"type": t, "key": key, "uri": real, "attrs": attrs, "store": "LOCAL_DISK" if real.startswith(("/tmp/", "/dev/shm/", "/var/tmp/")) else "PARALLEL_FS"}


def _fastp(path):
    out = {}
    try:
        d = json.load(open(path))
    except Exception:
        return out
    for part in ("before_filtering", "after_filtering"):
        for k in ("total_reads", "total_bases", "q20_rate", "q30_rate", "gc_content"):
            v = ((d.get("summary") or {}).get(part) or {}).get(k)
            if v is not None:
                out[part.split("_")[0] + "." + k] = v
    for k in ("passed_filter_reads", "low_quality_reads", "too_many_N_reads", "too_short_reads"):
        if (d.get("filtering_result") or {}).get(k) is not None:
            out["filter." + k] = d["filtering_result"][k]
    return out


def _seqkit(path):
    try:
        rows = [l.rstrip("\n").split("\t") for l in open(path) if l.strip()]
        idx = {h: i for i, h in enumerate(rows[0])}
    except Exception:
        return {}
    out, total = {"files": len(rows) - 1}, 0
    for n, r in enumerate(rows[1:]):
        for c in ("num_seqs", "sum_len", "min_len", "avg_len", "max_len"):
            if c in idx and idx[c] < len(r) and n < 8:
                out["%s.%s" % (os.path.basename(r[0]), c)] = r[idx[c]].replace(",", "")
        try:
            total += int(r[idx["num_seqs"]].replace(",", ""))
        except Exception:
            pass
    out["total_seqs"] = total
    return out


def _used(spec, alias, key, vals, cwd, root):
    out = []
    for i, it in enumerate(spec.get("used", [])):
        if not _ok(it, alias, key):
            continue
        kind, text = it.get("kind", "file"), vals.get(i, "").strip()
        for name in ([text] if kind == "db" else text.split()):
            path = name if os.path.isabs(name) else os.path.join(cwd, name)
            if not name or not (os.path.exists(path) or it.get("virtual")):
                continue
            real = os.path.realpath(path) if os.path.exists(path) else name
            m = _measure(real) if os.path.exists(path) else {}
            attrs = {"name": os.path.basename(real.rstrip("/")), **m}
            if kind == "db":
                attrs["database"] = it["name"]
            out.append(_ent(it["type"], "db/" + it["name"] if kind == "db" else _fkey(real, root, m.get("size", 0)), real, attrs))
    return out


def _generated(spec, alias, key, cwd, root, begin):
    out, seen = [], set()
    for rule in spec.get("generated", []):
        if not _ok(rule, alias, key):
            continue
        for path in sorted(glob.glob(os.path.join(glob.escape(cwd), rule["glob"].replace("{key}", key)), recursive=True)):
            name, real = os.path.basename(path), os.path.realpath(path)
            isdir = os.path.isdir(path)
            if name.startswith(".") or name == "versions.yml" or os.path.islink(path) or isdir != (rule.get("kind", "file") == "dir") or real in seen:
                continue
            m = _measure(real)
            if not isdir and m["mtime"] < begin - 2:
                continue
            seen.add(real)
            attrs = {"name": name, **m, **(rule.get("attrs") or {})}
            attrs.update(_fastp(real) if rule.get("parse") == "fastp_json" else _seqkit(real) if rule.get("parse") == "seqkit_tsv" else {})
            e = _ent(rule["type"], _fkey(real, root, m["size"]) if not isdir else (_under(real, root) or "ext/" + name), real, attrs)
            e["contains"] = rule.get("contains") or []
            out.append(e)
    return out


def plan(a):
    spec = (SPEC.get("processes") or {}).get(a.proc)
    if spec is None or (str(a.rc) != "0" and not spec.get("record_on_failure")):
        return None
    alias, key, cwd = a.full.rsplit(":", 1)[-1], _b64(a.key), os.getcwd()
    try:
        begin = os.stat(os.path.join(cwd, ".command.begin")).st_mtime
    except OSError:
        begin = time.time()
    vals = {int(k): _b64(v) for k, v in (s.split("=", 1) for s in a.used)}
    used, gen = _used(spec, alias, key, vals, cwd, a.root), _generated(spec, alias, key, cwd, a.root, begin)
    if not used and not gen:
        return None
    ins = {"cpus": a.cpus, "process": a.full}
    ins.update({"ext." + n: _b64(v) for n, v in (s.split("=", 1) for s in a.ext) if _b64(v)})
    if _b64(a.container):
        ins["container"] = _b64(a.container)
    job = next((os.environ[k] for k in ("SLURM_JOB_ID", "FLUX_JOB_ID") if os.environ.get(k)), "")
    outs = {"exit_status": a.rc, "host": socket.gethostname(), **({"job": job} if job else {})}
    sw = []
    try:
        for l in open(os.path.join(cwd, "versions.yml")).read().splitlines():
            if l.startswith((" ", "\t")) and ":" in l:
                k, v = l.strip().split(":", 1)
                if v.strip():
                    sw.append((k.strip(), v.strip().strip("'\"")))
    except OSError:
        pass
    if _b64(a.container):
        sw.append(("container", _b64(a.container)))
    rels = []
    for r in spec.get("relations", []):
        if _ok(r, alias, key):
            for s in (e for e in gen if e["type"] == r["subject"]):
                for o in (e for e in used if e["type"] == r["object"] and (not r.get("object_name") or e["attrs"].get("database") == r["object_name"])):
                    rels.append((r["rel"], s, o))
    for d in (e for e in gen if e["contains"]):
        rels += [("contains", d, o) for o in gen if o is not d and o["type"] in d["contains"]]
    return {"activity": alias, "begin_us": int(begin * 1e6), "end_us": int(time.time() * 1e6), "used": used, "generated": gen, "inputs": ins, "outputs": outs, "software": sw, "relations": rels}


def _vals(t, pairs):
    return [(t, hashlib.sha1((t + x).encode()).hexdigest()[:16], "MEMORY", x[:237] + "..." if len(x) > 240 else x) for x in ("%s=%s" % kv for kv in list(pairs.items())[:MAX_VALUES])]


def emit(p):
    import moirai
    if not moirai.initialize():
        return
    try:
        for t in SPEC.get("types", []):
            moirai.entity_type(t["name"], getattr(moirai.Role, t["role"].upper()), t["description"])
        for t in ("attribute", "input", "output"):
            moirai.entity_type(p["activity"] + "." + t if t != "attribute" else "nf_task.attribute", moirai.Role.PARAMETER if t == "input" else moirai.Role.REFERENCE if t == "attribute" else moirai.Role.INTERMEDIATE, "A %s value of an annotated Nextflow task, uri name=value." % t)
        tup = lambda e: (e["type"], e["key"], getattr(moirai.Store, e["store"]), e["uri"])  # noqa: E731
        R = moirai.Relation
        with moirai.Activity(p["activity"]) as act:
            try:
                act.span(p["begin_us"], p["end_us"])
            except Exception:
                pass
            for e in p["used"]:
                act.relate(R.USED, tup(e))
            for e in p["generated"]:
                act.relate(R.GENERATED, tup(e))
            for k, v in p["software"]:
                act.relate(R.USED, ("software", "%s@%s" % (k, v), moirai.Store.MEMORY, "%s=%s" % (k, v)))
            for v in _vals(p["activity"] + ".input", p["inputs"]):
                act.relate(R.USED, (v[0], v[1], moirai.Store.MEMORY, v[3]))
            for v in _vals(p["activity"] + ".output", p["outputs"]):
                act.relate(R.GENERATED, (v[0], v[1], moirai.Store.MEMORY, v[3]))
        for e in p["used"] + p["generated"]:
            for v in _vals("nf_task.attribute", e["attrs"]):
                moirai.relate(R.PART_OF, (v[0], v[1], moirai.Store.MEMORY, v[3]), tup(e))
        for rel, s, o in p["relations"]:
            r = getattr(R, rel.upper(), None)
            if r is not None:
                moirai.relate(r, tup(s), tup(o))
    finally:
        moirai.finalize()


def main(argv):
    try:
        ap = argparse.ArgumentParser(add_help=False)
        for n in ("cmd", "proc", "full", "rc"):
            ap.add_argument(n)
        for n in ("key", "cpus", "container", "root"):
            ap.add_argument("--" + n, default="")
        for n in ("ext", "used"):
            ap.add_argument("--" + n, action="append", default=[])
        a = ap.parse_args(argv)
        p = plan(a)
        if p is not None and a.cmd == "nf-plan":
            print(json.dumps(p, default=str, sort_keys=True))
        elif p is not None:
            emit(p)
    except BaseException as e:
        sys.stderr.write("moirai nf-task skipped: %s\n" % (e,))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _helper_text(spec):
    keep = {"types": [{k: t[k] for k in ("name", "role", "description")} for t in spec["types"]], "processes": spec["processes"]}
    return HELPER_SRC.replace("__SPEC__", base64.b64encode(json.dumps(keep, sort_keys=True).encode()).decode())


def _overlay():
    names = ["MOIRAI_PYTHON", "MOIRAI_SERVICE", "MOIRAI_SERVICE_SHM", "MOIRAI_LOG_FILE", "MOIRAI_NF_LOG", "DFTRACER_LOG_FILE", "DFTRACER_DATA_DIR"]
    L = ["// moirai_overlay.config: generated by nextflow_annotate_apply; do not edit. Use: nextflow run main.nf -c moirai_overlay.config ...",
         "// No user name and no machine path is written here: the job script exports the variables (moirai/job_lines.sh) and they are read at run time.",
         "env {", '    MOIRAI_NF_HELPER = "${projectDir}/moirai/_moirai_nf.py"', "    DFTRACER_ENABLE = System.getenv('DFTRACER_ENABLE') ?: '1'", "    DFTRACER_INC_METADATA = '1'", "}"]
    L += ["if (System.getenv('%s')) { env.%s = System.getenv('%s') }" % (n, n, n) for n in names]
    L += ["if (System.getenv('MOIRAI_DFTRACER_PRELOAD')) { env.LD_PRELOAD = System.getenv('MOIRAI_DFTRACER_PRELOAD') }",
          "// a container needs the helper, the moirai python tree and the moirai ring (/dev/shm); this REPLACES an existing runOptions, so the old one goes in MOIRAI_BASE_RUN_OPTIONS",
          "if (System.getenv('MOIRAI_PYTHON')) {",
          "    def moiraiBinds = (System.getenv('MOIRAI_BIND_PATHS') ?: '').tokenize(':') + ['/dev/shm', \"${projectDir}/moirai\".toString()]",
          "    def moiraiBase = System.getenv('MOIRAI_BASE_RUN_OPTIONS') ?: ''",
          "    podman.runOptions = (moiraiBase + ' ' + moiraiBinds.collect { \"-v ${it}:${it}\".toString() }.join(' ')).trim()",
          "    singularity.runOptions = '--bind ' + moiraiBinds.join(',')", "    apptainer.runOptions = '--bind ' + moiraiBinds.join(',')", "}"]
    return "\n".join(L) + "\n"


def _job_lines(launcher="srun"):
    per = 'srun --nodes="${SLURM_JOB_NUM_NODES:-1}" --ntasks-per-node=1' if launcher == "srun" else 'flux run -N "${NODES:-1}" --tasks-per-node=1'
    return "\n".join(["# Lines for the job script (placeholders: set MOIRAI_PYTHON and MOIRAI_TRACE_DIR from the session before these).",
                      'export MOIRAI_PYTHON="${MOIRAI_PYTHON:?the python of the session moirai venv}"', 'export MOIRAI_SERVICE="${MOIRAI_SERVICE:-$(dirname "$MOIRAI_PYTHON")/moirai-service}"',
                      'export MOIRAI_TRACE_DIR="${MOIRAI_TRACE_DIR:?a trace directory under the session}"; mkdir -p "$MOIRAI_TRACE_DIR"',
                      'export MOIRAI_LOG_FILE="$MOIRAI_TRACE_DIR/moirai" MOIRAI_NF_LOG="$MOIRAI_TRACE_DIR/nf_hook.log" MOIRAI_SERVICE_SHM="/moirai-prov-${SLURM_JOB_ID:-$$}"',
                      'export DFTRACER_ENABLE=1 DFTRACER_LOG_FILE="$MOIRAI_TRACE_DIR/dftracer" DFTRACER_INC_METADATA=1',
                      'export MOIRAI_BIND_PATHS="$(dirname "$(dirname "$MOIRAI_PYTHON")"):$(dirname "$(dirname "$(readlink -f "$MOIRAI_PYTHON")")")"',
                      "# one moirai-service per node, before nextflow; stop it after, also on TERM",
                      f'timeout 60 {per} "$MOIRAI_SERVICE" start || true', 'nextflow run main.nf -c moirai_overlay.config "$@" &', "NFPID=$!",
                      f"trap 'kill -TERM $NFPID; wait $NFPID; {per} \"$MOIRAI_SERVICE\" stop; exit 143' TERM", "wait $NFPID; RC=$?", f'{per} "$MOIRAI_SERVICE" stop || true', "exit $RC"]) + "\n"


def _files(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if not f.endswith(".swp"):
                yield Path(dp, f)


def _diff(src, dst):
    out = []
    for f in sorted(_files(dst)):
        rel, o = f.relative_to(dst), Path(src, f.relative_to(dst))
        if not o.is_file():
            out.append(f"new file: {rel}\n")
        elif f.suffix == ".nf" and f.read_text(errors="replace") != o.read_text(errors="replace"):
            out += difflib.unified_diff(o.read_text(errors="replace").split("\n"), f.read_text(errors="replace").split("\n"), f"a/{rel}", f"b/{rel}", lineterm="")
            out.append("\n")
    return "\n".join(out)


# ---- diagram -------------------------------------------------------------------------------------------------------------------------
def _diagram(spec):
    nodes, edges, seen, fe, re_ = [], [], set(), set(), set()
    ids = {"flow": 0, "relation": 0}

    def node(i, label, kind, parent=None, info=None):
        if i not in seen:
            seen.add(i)
            nodes.append({"id": i, "label": label, "kind": kind, **({"parent": parent} if parent else {}), **({"info": info} if info else {})})

    def edge(s, t, label, kind):
        if (s, t, label) not in (fe if kind == "flow" else re_):
            (fe if kind == "flow" else re_).add((s, t, label))
            edges.append({"id": ("e%d" if kind == "flow" else "r%d") % ids[kind], "source": s, "target": t, "label": label, "kind": kind, "conditional": False})
            ids[kind] += 1

    ok = lambda it, al: not it.get("only") or al in it["only"]  # noqa: E731
    acts = [(al, e, a) for n, e in spec["processes"].items() for al, a in (e.get("aliases") or {n: {}}).items()]
    types = {t["name"]: t for t in spec["types"]}
    stage_of = {}
    for al, e, _a in acts:
        for g in e["generated"]:
            if ok(g, al):
                stage_of.setdefault(g["type"], e["stage"])
    for al, e, _a in acts:
        for u in e["used"]:
            if ok(u, al) and u["kind"] != "db":
                stage_of.setdefault(u["type"], e["stage"])
    dbs = sorted({u["name"] for _al, e, _a in acts for u in e["used"] if u["kind"] == "db"})
    imgs = sorted({e["image"] for _al, e, _a in acts if e.get("image")})
    for g in STAGES:
        node(g, STAGES[g], "activity")
    node("g_db", "Databases (%d)" % len(dbs), "reference")
    node("g_sw", "Software: container images", "reference")
    for t, st in stage_of.items():
        node("e_" + t, types[t]["label"], types[t]["role"], st, types[t]["description"])
    for d in dbs:
        node("d_" + d, d, "reference", "g_db")
    for im in imgs:
        node("s_" + re.sub(r"\W+", "_", im), "image: " + im, "reference", "g_sw")
    for al, e, a in acts:
        pid = "p_" + a.get("short", e["short"])
        node(pid, e.get("label", al), "activity", e["stage"], a.get("info") or e.get("info"))
        for u in e["used"]:
            if ok(u, al):
                edge(("d_" + u["name"]) if u["kind"] == "db" else "e_" + u["type"], pid, u.get("label", "used"), "flow")
        for g in e["generated"]:
            if ok(g, al):
                edge(pid, "e_" + g["type"], "generated", "flow")
        if e.get("image"):
            edge("s_" + re.sub(r"\W+", "_", e["image"]), pid, "image", "flow")
        for r in e["relations"]:
            if ok(r, al):
                edge("e_" + r["subject"], ("d_" + r["object_name"]) if r["object"] == "reference_database" else "e_" + r["object"], r["label"], "relation")
    counts = {"processes": len(acts), "entity_types": sum(1 for n in nodes if n["id"].startswith("e_")), "databases": len(dbs), "images": len(imgs), "flow_edges": ids["flow"], "relation_edges": ids["relation"]}
    legend = [{"kind": "flow", "label": "used / generated: data read and written by an activity"}, {"kind": "relation", "label": "relationship between entities: derived from, classified against"}]
    return {"nodes": nodes, "edges": edges, "legend": legend}, counts


# ---- validation ----------------------------------------------------------------------------------------------------------------------
def _check_tree(src, dst, spec):
    """Pure: [(name, True|False|None, detail, fix)] for the annotated copy `dst` of `src`."""
    R, procs = [], spec["processes"]
    R.append(("applied", Path(dst).is_dir(), "no annotated copy", "run nextflow_annotate_apply"))
    if not Path(dst).is_dir():
        return R
    by_file, blocks = {}, {}
    for n, e in procs.items():
        by_file.setdefault(e["file"], {})[n] = e
    for f in _files(dst):
        if f.suffix == ".nf":
            for n, b in _blocks(f.read_text(errors="replace")).items():
                blocks.setdefault(n, []).append((str(f.relative_to(dst)), b))
    bad = [n for n in procs if len(blocks.get(n, [])) != 1 or len(blocks[n][0][1]) != 1 or blocks[n][0][0] != procs[n]["file"]] + [n for n in blocks if n not in procs]
    R.append(("one_block", not bad, f"not exactly one block in its own file: {bad}", "re-run apply (it strips and rewrites every block)"))
    anchor, lint, names, intact = [], [], [], []
    for rel, ents in by_file.items():
        o, a = Path(src, rel), Path(dst, rel)
        if not (o.is_file() and a.is_file()):
            anchor.append(rel)
            continue
        lines, al = _strip(a.read_text(errors="replace")).split("\n"), a.read_text(errors="replace").split("\n")
        for n, e in ents.items():
            s, t = _find(lines, n)
            op = _opener(lines, s, t)[0] if s is not None else None
            if op is None or not al[op].strip() == '"""' or not al[op + 1].lstrip().startswith(OPEN + " " + n):
                anchor.append(f"{n}")
            elif [r for x in [e["key"]] + [u["expr"] for u in e["used"]] for r in _roots(x) if r not in _avail(lines, s, op)]:
                names.append(n)
            for b in blocks.get(n, [])[:1]:
                lint += [f"{n}: {p}" for p in _lint(b[1][0])]
    R += [("anchor", not anchor, f"block not directly after the script's opening triple quote: {anchor}", "fix the process name or file in the spec; apply again"),
          ("escapes_bash", not lint, "; ".join(lint)[:600], "fix the hook template in the tool (never edit the .nf), apply again"),
          ("names_resolve", not names, f"expressions name something that is not an input: {names}", "fix the spec expression (overrides), apply again")]
    for f in _files(src):
        rel, a = f.relative_to(src), Path(dst, f.relative_to(src))
        same = a.is_file() and (_strip(a.read_text(errors="replace")) == f.read_text(errors="replace") if f.suffix == ".nf" else a.read_bytes() == f.read_bytes())
        if not same:
            intact.append(str(rel))
    R.append(("source_intact", not intact, f"minus its blocks the copy differs from the source: {intact[:5]}", "apply again; never edit the copy by hand"))
    extra = [str(f.relative_to(dst)) for f in _files(dst) if not Path(src, f.relative_to(dst)).is_file() and f.relative_to(dst).parts[0] != "moirai" and f.name != "moirai_overlay.config"]
    R.append(("stray_files", not extra, f"files in the copy that are not the source's: {extra[:5]}", "remove them or re-copy"))
    h, o, j = Path(dst, "moirai/_moirai_nf.py"), Path(dst, "moirai_overlay.config"), Path(dst, "moirai/job_lines.sh")
    try:
        ast.parse(h.read_text())
        okh = True
    except (OSError, SyntaxError):
        okh = False
    R.append(("helper", okh, "moirai/_moirai_nf.py is missing or does not parse", "run apply"))
    ot = o.read_text() if o.is_file() else ""
    R.append(("overlay", ot.count("{") == ot.count("}") and "System.getenv" in ot and "runOptions" in ot, "moirai_overlay.config missing or unbalanced", "run apply"))
    texts = [p.read_text() for p in (h, o, j) if p.is_file()] + [b for v in blocks.values() for _f, bl in v for b in bl]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    leak = [t[:0] + (PRIV.search(t).group(0) if PRIV.search(t) else "user name") for t in texts if PRIV.search(t) or (len(user) > 2 and user in t)]
    R.append(("privacy", not leak, f"generated text names a machine path or the user: {leak[:3]}", "use a run-time placeholder"))
    return R


def _nf_parse(nextflow, root, version, args):
    env = dict(os.environ, NXF_VER=version)
    try:
        r = subprocess.run([nextflow, "run", str(Path(root, "main.nf")), "-preview", *args], cwd=str(root), env=env, capture_output=True, text=True, timeout=600)
        return r.returncode, (r.stdout + r.stderr)[-400:]
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


# ---- preview and selftest ------------------------------------------------------------------------------------------------------------------
FAKE = '''import json, os
class Role: INPUT = "INPUT"; OUTPUT = "OUTPUT"; INTERMEDIATE = "INTERMEDIATE"; PARAMETER = "PARAMETER"; REFERENCE = "REFERENCE"; UNKNOWN = "UNKNOWN"
class Store: MEMORY = "MEMORY"; LOCAL_DISK = "LOCAL_DISK"; PARALLEL_FS = "PARALLEL_FS"; NETWORK = "NETWORK"; OTHER = "OTHER"
class Relation:
    USED = "USED"; GENERATED = "GENERATED"; DERIVED_FROM = "DERIVED_FROM"; DEPENDS_ON = "DEPENDS_ON"; CONTAINS = "CONTAINS"; PART_OF = "PART_OF"
S = {"events": [], "links": [], "types": [], "final": 0}
def _dump():
    json.dump(S, open(os.environ["MOIRAI_FAKE_OUT"], "w"))
def initialize(log_file=""): return True
def finalize(): S["final"] += 1; _dump()
def entity_type(name, role, description=""): S["types"].append([name, role])
def relate(rel, s, o): S["links"].append([rel, s[0], s[1], s[3], o[0], o[1]])
class Activity:
    def __init__(self, name): self.name, self.rows, self.sp = name, [], None
    def __enter__(self): return self
    def __exit__(self, *a): S["events"].append({"activity": self.name, "span": self.sp, "relations": self.rows}); _dump(); return False
    def relate(self, rel, e): self.rows.append([rel, e[0], e[1], e[3]]); return e
    def span(self, a, b): self.sp = [int(a), int(b)]
'''


def _preview(files, process, task=None, spec=None):
    task, entry = task or {}, ((spec or {}).get(process) or CATALOG.get(process))
    if entry is None or entry["file"] not in files:
        return {"status": "error", "problems": [f"no spec for {process} or no text for its file"]}
    text, probs = _apply_text(files[entry["file"]], {process: entry})
    if probs:
        return {"status": "error", "problems": probs}
    blk = _blocks(text)[process][0].split("\n")
    with tempfile.TemporaryDirectory() as d:
        base = Path(d).resolve()
        root, wd = base / "work", base / "work" / task.get("workdir", "ab/cd0001")
        hd = base / "app" / "moirai"
        hd.mkdir(parents=True)
        (hd / "_moirai_nf.py").write_text(_helper_text({"types": TYPES, "processes": {process: entry}}))
        (hd / "moirai.py").write_text(FAKE)
        wd.mkdir(parents=True)
        for rel, body in (task.get("outside") or {}).items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(body)
        (wd / ".command.begin").write_text("")
        os.utime(wd / ".command.begin", (time.time() - 100, time.time() - 100))
        for rel, body in (task.get("files") or {}).items():
            (wd / rel).parent.mkdir(parents=True, exist_ok=True)
            (wd / rel).write_text(body)
        for name, tgt in (task.get("links") or {}).items():
            os.symlink(base / tgt, wd / name)
        alias = task.get("alias", process)
        vals = {"task.process": "NFCORE:WF:" + alias, "task.cpus": "4", "task.container": "null", "workflow.workDir": str(root), **(task.get("values") or {})}
        bash, bp, missing = _render("\n".join(blk), vals)
        if bp or missing:
            return {"status": "error", "problems": bp + [f"values lacks {m}" for m in missing]}
        (wd / "run.sh").write_text(bash + "\n" + task.get("body", "true") + "\n")
        env = {k: os.environ[k] for k in ("PATH", "LD_LIBRARY_PATH", "LANG") if k in os.environ}
        env.update({"HOME": str(base), "MOIRAI_PYTHON": sys.executable, "MOIRAI_NF_HELPER": str(hd / "_moirai_nf.py"), "MOIRAI_FAKE_OUT": str(base / "events.json")})
        env.update(task.get("env") or {})
        r = subprocess.run(["bash", "-C", "-e", "-u", "-o", "pipefail", str(wd / "run.sh")], cwd=str(wd), env=env, capture_output=True, text=True, timeout=90)
        got = json.loads((base / "events.json").read_text()) if (base / "events.json").is_file() else {"events": [], "links": []}
        return {"status": "ok", "rc": r.returncode, "stdout": r.stdout[-300:], "stderr": r.stderr[-300:], "events": got["events"], "links": got["links"], "block": "\n".join(blk)}


_FASTP = '''process FASTP {
    tag "$meta.id"
    input:
    tuple val(meta), path(reads)
    path  adapter_fasta
    output:
    path "versions.yml", emit: versions
    script:
    def args = task.ext.args ?: ''
    """
    fastp --in1 ${reads[0]}
    """
    stub:
    """
    touch x
    """
}
'''
_KAIJU = '''process KAIJU_KAIJU {
    input:
    tuple val(meta), path(reads)
    path(db)
    script:
    def args = task.ext.args ?: ''
    """
    kaiju -f $db $reads
    """
}

process KAIJU_ADDTAXONNAMES {
    input:
    tuple val(meta), path(kaiju)
    path(db)
    script:
    """
    kaiju-addTaxonNames -i $kaiju
    """
}
'''
_RCF = r'''process RECENTRIFUGE {
    input:
    tuple val(meta), path(results), path(db), val(input_type_str)
    script:
    def f = (input_type_str == "x") ?
        "-f $results" :
        """--format "T" \\
    -g $results"""
    """
    rcf -n $db
    """
    stub:
    """
    touch a
    """
}
'''


def _selftest():
    R = []

    def chk(name, ok, detail=""):
        R.append((name, bool(ok), str(detail)[:300]))

    b, pr, _m = _render('echo $x \\n ${task.cpus}')
    chk("lex_flags_bare_dollar_and_escape", len(pr) == 2, pr)
    chk("lex_escaped_dollar", _render("echo \\$HOME ${task.cpus}", {"task.cpus": "7"})[0] == "echo $HOME 7")
    text, pr = _apply_text(_RCF, {"RECENTRIFUGE": CATALOG["RECENTRIFUGE"]})
    ls = text.split("\n")
    at = next(i for i, l in enumerate(ls) if OPEN in l)
    chk("anchor_skips_def_string", not pr and ls[at - 1].strip() == '"""' and ls[at - 2].strip() == '""""' or (not pr and "rcf -n" in ls[next(i for i, l in enumerate(ls) if CLOSE in l) + 1]), pr or ls[at - 2:at + 1])
    chk("stub_untouched_and_one_block", text.count(OPEN) == 1 and "touch a" in text.split(CLOSE)[1])
    chk("strip_restores_source", _strip(text) == _RCF and _apply_text(text, {"RECENTRIFUGE": CATALOG["RECENTRIFUGE"]})[0] == text)
    chk("lint_clean", not _lint(_blocks(text)["RECENTRIFUGE"][0]), _lint(_blocks(text)["RECENTRIFUGE"][0]))
    chk("unknown_name_refused", _apply_text(_FASTP, {"FASTP": {**CATALOG["FASTP"], "key": "nope.id"}})[1] != [])
    chk("own_trap_refused", _apply_text(_FASTP.replace("fastp --in1", "trap x EXIT"), {"FASTP": CATALOG["FASTP"]})[1] != [])
    fp = json.dumps({"summary": {"before_filtering": {"total_reads": 7771234}, "after_filtering": {"total_reads": 7001789}}})
    t1 = {"alias": "FASTP", "values": {"meta.id": "zq9w", "reads": "zq9w_1.fastq.gz"}, "outside": {"raw/zq9w_a.fq.gz": "x" * 37}, "links": {"zq9w_1.fastq.gz": "raw/zq9w_a.fq.gz"},
          "files": {"zq9w.fastp.json": fp, "zq9w_1.fastp.fastq.gz": "y" * 11, "zq9w.fastp.html": "<html>", "versions.yml": '"NFCORE:WF:FASTP":\n    fastp: 0.99.7\n'}}
    r = _preview({CATALOG["FASTP"]["file"]: _FASTP}, "FASTP", t1)
    rel = [x for e in r.get("events", []) for x in e["relations"]]
    chk("fastp_records_entities", r.get("rc") == 0 and ["USED", "raw_reads", "ext/zq9w_a.fq.gz@37"] == rel[0][:3] and any(x[:3] == ["GENERATED", "trimmed_reads", "ab/cd0001/zq9w_1.fastp.fastq.gz"] for x in rel), r.get("problems") or rel[:3])
    chk("fastp_counts_and_software", any(l[3] == "before.total_reads=7771234" for l in r.get("links", [])) and any(x[:3] == ["USED", "software", "fastp@0.99.7"] for x in rel))
    chk("fastp_relation", any(l[0] == "DERIVED_FROM" and l[1] == "fastp_report" for l in r.get("links", [])))
    r3 = _preview({CATALOG["FASTP"]["file"]: _FASTP}, "FASTP", {**t1, "body": "exit 3"})
    chk("exit_status_kept_and_failure_not_recorded", r3.get("rc") == 3 and r3.get("events") == [], r3.get("rc"))
    rb = _preview({CATALOG["FASTP"]["file"]: _FASTP}, "FASTP", {**t1, "env": {"MOIRAI_PYTHON": "/nonexistent/python"}})
    chk("broken_moirai_changes_nothing", rb.get("rc") == 0 and rb.get("events") == [] and not rb.get("stdout"), rb.get("rc"))
    k1 = {"values": {"meta.id": "zq9w", "reads": "zq9w_1.fq.gz", "db": "kaiju_db"}, "outside": {"raw/r1.fq": "r" * 5, "dbs/kaiju_db/a.fmi": "z"}, "links": {"zq9w_1.fq.gz": "raw/r1.fq", "kaiju_db": "dbs/kaiju_db"}, "files": {"zq9w.kaiju.tsv": "k" * 5}}
    k2 = {"workdir": "ab/cd0002", "values": {"meta.id": "zq9w", "kaiju": "zq9w.kaiju.tsv", "db": "kaiju_db"}, "outside": {"work/ab/cd0001/zq9w.kaiju.tsv": "k" * 5, "dbs/kaiju_db/a.fmi": "z"},
          "links": {"zq9w.kaiju.tsv": "work/ab/cd0001/zq9w.kaiju.tsv", "kaiju_db": "dbs/kaiju_db"}, "files": {"zq9w.names.tsv": "n"}}
    f = {CATALOG["KAIJU_KAIJU"]["file"]: _KAIJU}
    a, c = _preview(f, "KAIJU_KAIJU", k1), _preview(f, "KAIJU_ADDTAXONNAMES", k2)
    ga = [x for e in a.get("events", []) for x in e["relations"] if x[0] == "GENERATED" and x[1] == "kaiju_results"]
    uc = [x for e in c.get("events", []) for x in e["relations"] if x[0] == "USED" and x[1] == "kaiju_results"]
    chk("hop_joins_by_type_and_key", ga and uc and ga[0][2] == uc[0][2] == "ab/cd0001/zq9w.kaiju.tsv", (ga, uc, a.get("problems"), c.get("problems")))
    chk("database_entity", any(x[:3] == ["USED", "reference_database", "db/kaiju_db"] for e in c.get("events", []) for x in e["relations"]))
    d, counts = _diagram({"types": TYPES, "processes": json.loads(json.dumps(CATALOG))})
    chk("diagram_matches_approved", counts == {"processes": 16, "entity_types": 18, "databases": 10, "images": 9, "flow_edges": 60, "relation_edges": 27} and len(d["nodes"]) == 58 and len(d["edges"]) == 87, counts)
    spec = {"version": 1, "types": TYPES, "processes": CATALOG}
    chk("helper_parses_no_leak", ast.parse(_helper_text(spec)) is not None and not PRIV.search(_helper_text(spec)) and not PRIV.search(_overlay()) and not PRIV.search(_job_lines()))
    chk("overlay_balanced", _overlay().count("{") == _overlay().count("}") and "runOptions" in _overlay())
    with tempfile.TemporaryDirectory() as t:
        src, dst = Path(t, "s"), Path(t, "d")
        (src / "modules/nf-core/fastp").mkdir(parents=True)
        (src / "modules/nf-core/fastp/main.nf").write_text(_FASTP)
        shutil.copytree(src, dst)
        new, pr = _apply_text(_FASTP, {"FASTP": CATALOG["FASTP"]})
        (dst / "modules/nf-core/fastp/main.nf").write_text(new)
        (dst / "moirai").mkdir()
        (dst / "moirai/_moirai_nf.py").write_text(_helper_text(spec))
        (dst / "moirai_overlay.config").write_text(_overlay())
        sp = {"processes": {"FASTP": CATALOG["FASTP"]}}
        rows = _check_tree(src, dst, sp)
        chk("validator_passes_clean_tree", all(x[1] for x in rows), [x[0] for x in rows if not x[1]])
        (dst / "modules/nf-core/fastp/main.nf").write_text(new.replace("fastp --in1", "fastp --IN1"))
        chk("validator_catches_hand_edit", any(x[0] == "source_intact" and not x[1] for x in _check_tree(src, dst, sp)))
    return R


def register(mcp):
    def _saved(c):
        f = P.work(c) / "nf_spec.json"
        return json.loads(f.read_text()) if f.is_file() else None

    @mcp.tool()
    def nextflow_annotate_spec(pipeline: str, exclude: list = None, overrides: dict = None) -> dict:
        """Per-process annotation spec of a Nextflow pipeline, from the entity plan agreed in <session>/provenance/discovery.md and the module scripts of the session source. Use first, before the diagram.
        pipeline: pipeline id. exclude: process names to leave out. overrides: {process: {field: value}} patches, or {process: null} to drop one. Saves nf_spec.json in the provenance dir.
        Returns {status, spec: {types, processes}, processes, notes: {excluded, missing (catalog process not in the source), unknown (a process the spec does not cover), not_in_discovery}}."""
        c = P.load(pipeline)
        d = P.work(c) / "discovery.md"
        spec, notes = _build_spec(P.source_of(c), d.read_text() if d.is_file() else "", exclude or [], overrides)
        (P.work(c) / "nf_spec.json").write_text(json.dumps(spec, indent=1))
        return {"status": "ok", "spec": spec, "processes": sorted(spec["processes"]), "notes": notes}

    @mcp.tool()
    def nextflow_annotate_diagram(pipeline: str) -> dict:
        """Nodes and edges of what is annotated, ready for diagram_publish_graph(pipeline, stage, name, title, graph). Use at the annotate gate. Entity-type nodes (by role), activity nodes (MINIMAP_A and MINIMAP_B apart) grouped by stage,
        database and container-image nodes, used/generated/image edges (kind flow) and relation edges between entities (kind relation: derived_from, depends_on with labels such as classified against). pipeline: pipeline id (uses nf_spec.json, else the catalog).
        Returns {status, graph: {nodes, edges, legend}, counts: {processes, entity_types, databases, images, flow_edges, relation_edges}}."""
        c = P.load(pipeline)
        g, counts = _diagram(_saved(c) or _build_spec(P.source_of(c))[0])
        return {"status": "ok", "graph": g, "counts": counts}

    @mcp.tool()
    def nextflow_annotate_apply(pipeline: str, spec: dict = None, launcher: str = "srun") -> dict:
        """Copy the pipeline once to <provenance>/source, then (re)write the marked hook blocks, moirai/_moirai_nf.py (the helper), moirai_overlay.config (env, container bind mounts) and moirai/job_lines.sh (dftracer with moirai, one
        moirai-service per node). Safe to repeat: every block is stripped and rewritten. The source tree is never touched. pipeline: pipeline id. spec: the spec (default: nf_spec.json, else built). launcher: srun|flux for job_lines.
        Returns {status, annotated, files, problems, existing_run_options (the overlay replaces them), diff (only the marked blocks), job_lines}."""
        c = P.load(pipeline)
        src, dst = Path(P.source_of(c)), P.work(c) / "source"
        spec = spec or _saved(c) or _build_spec(src)[0]
        if not dst.is_dir():
            shutil.copytree(src, dst, ignore=SKIP, symlinks=True)
        by, problems, done = {}, [], []
        for n, e in spec["processes"].items():
            by.setdefault(e["file"], {})[n] = e
        for rel, ents in by.items():
            f = dst / rel
            if not f.is_file():
                problems.append(f"{rel}: not in the copy")
                continue
            new, pr = _apply_text(f.read_text(errors="replace"), ents)
            problems += pr
            if not pr:
                f.write_text(new)
                done.append(rel)
        (dst / "moirai").mkdir(exist_ok=True)
        (dst / "moirai/_moirai_nf.py").write_text(_helper_text(spec))
        (dst / "moirai/job_lines.sh").write_text(_job_lines(launcher))
        (dst / "moirai_overlay.config").write_text(_overlay())
        old = [m.group(0) for f in list(dst.glob("*.config")) + list((dst / "conf").glob("*.config")) for m in re.finditer(r"\w+\.runOptions\s*=\s*.+", f.read_text(errors="replace")) if f.name != "moirai_overlay.config"]
        diff = _diff(src, dst)
        (P.work(c) / "nf_annotation.diff").write_text(diff)
        (P.work(c) / "nf_spec.json").write_text(json.dumps(spec, indent=1))
        P.record(pipeline, STAGE, outputs={"annotated": str(dst), "files": sorted(done), "languages": ["nextflow"]}, note=f"annotated {len(done)} file(s)")
        return {"status": "ok" if not problems else "error", "annotated": str(dst), "files": sorted(done), "problems": problems, "existing_run_options": old, "diff": diff[:12000], "job_lines": _job_lines(launcher)}

    @mcp.tool()
    def nextflow_annotate_validate(pipeline: str, nextflow: str = "", nf_version: str = "25.04.6", nf_args: list = None) -> dict:
        """Validate the annotated copy: one block per process in its own file, directly after the script's opening triple quote; Groovy escapes (no bare $, no stray backslash), the rendered block passes bash -n and never calls exit;
        every name resolves to a process input; minus its blocks the copy is the source byte for byte; helper parses; overlay balanced; no machine path or user name in generated text. nextflow: path of the launcher to also run
        `-preview` on the copy and on a temp copy of the source (a failure that the source shows too is inconclusive); without it that check is listed under deferred. nf_version: NXF_VER. nf_args: extra args, for example ["-profile","test"].
        Returns the verdict {status, failed, unknown, passed, total, deferred}."""
        c = P.load(pipeline)
        spec = _saved(c)
        v = P.Verdict(STAGE)
        if not v.check("spec", bool(spec), "nextflow_annotate_spec has not run", "run nextflow_annotate_spec"):
            return P.finish_validation(pipeline, v)
        dst, src, deferred = P.work(c) / "source", Path(P.source_of(c)), []
        for n, ok, detail, fix in _check_tree(src, dst, spec):
            v.check(n, ok, detail, fix)
        if nextflow:
            a = _nf_parse(nextflow, dst, nf_version, nf_args or [])
            with tempfile.TemporaryDirectory() as t:
                shutil.copytree(src, Path(t, "s"), ignore=SKIP, symlinks=True)
                o = _nf_parse(nextflow, Path(t, "s"), nf_version, nf_args or [])
            if a[0] == 0 or o[0] not in (0,):
                (v.check("nextflow_parse", True, "", "") if a[0] == 0 else v.inconclusive("nextflow_parse", f"the source does not preview either: {a[1]}"))
            else:
                v.check("nextflow_parse", False, f"annotated copy fails -preview, source passes: {a[1]}", "apply one process at a time (exclude) to find the block")
        else:
            deferred.append({"name": "nextflow_parse", "detail": "no launcher given: only the deterministic text checks ran"})
        r = P.finish_validation(pipeline, v)
        r["deferred"] = deferred
        return r

    @mcp.tool()
    def nextflow_annotate_preview(files: dict, process: str, task: dict = None, spec: dict = None) -> dict:
        """Run one process's hook the way Nextflow would, with no pipeline: annotate the module text, render the Groovy, run it under bash -C -e -u -o pipefail in a fixture task directory against a fake moirai.
        files: {relative path: module text}, must hold the process's file. process: module process name (CATALOG or spec). spec: {process: entry} overriding the catalog. task: {alias (MINIMAP_B), workdir ("ab/cd0001"), values {groovy
        expression: text}, files {name: text in the task dir}, outside {path relative to the fixture root: text}, links {name: target relative to the fixture root}, body (bash run after the hook, e.g. "exit 3"), env (e.g.
        {"MOIRAI_PYTHON": "/nonexistent/python"} to break moirai on purpose)}. Returns {status, rc, stdout, stderr, events: [{activity, span, relations: [[REL, type, key, uri]]}], links, block} or {status: error, problems}."""
        return _preview(files, process, task, spec)

    @mcp.tool()
    def nextflow_annotate_selftest() -> dict:
        """Prove the tool itself on fixtures whose values cannot overlap: Groovy escaping, anchor that skips a def string, idempotent strip, exit status and caching text unchanged, a broken moirai changing nothing, entities and counts recorded,
        a hop joining by type and key, the diagram equal to the approved one (16 processes, 10 databases, 9 images, 18 entity types, 60 flow and 27 relation edges), no privacy leak, validator catching a hand edit.
        Returns {status: ok|error, passed, total, failed: [{name, detail}]}."""
        rows = _selftest()
        bad = [{"name": n, "detail": d} for n, ok, d in rows if not ok]
        return {"status": "ok" if not bad else "error", "passed": len(rows) - len(bad), "total": len(rows), "failed": bad}
