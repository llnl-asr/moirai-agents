"""Run pipeline stage 3: find the scientific artifact and every place the application touches data, before anything is decided.

Moved from dftracer-agents' provenance mode. It never picks the entities: it gathers the evidence and the questions for the user.
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

from moirai_agents.core import pipeline as P

STAGE = "discover"
CASES = Path(__file__).resolve().parent / "CASES.md"
SKIP_DIRS = {".git", "build", "_build", "__pycache__", ".venv", "venv", "node_modules", "CMakeFiles", "traces"}
PY_EXT = {".py"}
C_EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".C", ".hpp", ".hh", ".hxx", ".cu", ".hip"}
FUNC = {"py": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("), "c": re.compile(r"^[A-Za-z_][\w\s\*:<>,&~]*?\b(\w+)\s*\([^;]*$")}
NOT_FUNCS = {"if", "for", "while", "switch", "return", "sizeof", "catch", "elif"}

SCIENCE_EXT = {
    ".pdb": "molecular structure", ".cif": "molecular structure", ".mmcif": "molecular structure",
    ".sdf": "molecule", ".mol2": "molecule", ".xyz": "atomic coords", ".gro": "MD structure",
    ".xtc": "MD trajectory", ".trr": "MD trajectory", ".dcd": "MD trajectory", ".nc": "netCDF field",
    ".h5": "HDF5 dataset", ".hdf5": "HDF5 dataset", ".he5": "HDF5 dataset", ".bp": "ADIOS2 data",
    ".vtk": "mesh/field", ".vtu": "mesh/field", ".pvtu": "mesh/field", ".xdmf": "mesh/field",
    ".silo": "mesh/field", ".exo": "mesh", ".e": "mesh", ".fits": "astronomy image",
    ".vcf": "variants", ".bam": "alignments", ".sam": "alignments", ".fa": "sequence",
    ".fasta": "sequence", ".fastq": "reads", ".a3m": "MSA", ".sto": "MSA",
    ".npy": "array", ".npz": "arrays", ".pt": "model/tensor", ".pth": "model/tensor",
    ".ckpt": "checkpoint", ".safetensors": "model weights", ".onnx": "model",
    ".csv": "table", ".parquet": "table", ".json": "record", ".sqlite": "database",
    ".db": "database", ".zarr": "array store", ".tif": "image", ".png": "image/plot",
}

# (regex, kind, family) -- kind: read|write|open|create|db|exec (access/store),
# communicate (data moving between ranks/devices/services), compute (in-memory transform)
PATTERNS = [
    # C / C++
    (r"\bfopen\s*\(", "open", "c"), (r"\bopen\s*\(", "open", "c"),
    (r"\bfwrite\s*\(", "write", "c"), (r"\bfread\s*\(", "read", "c"),
    (r"\bH5Fcreate\s*\(", "create", "c"), (r"\bH5Fopen\s*\(", "open", "c"),
    (r"\bH5Dcreate\d?\s*\(", "create", "c"), (r"\bH5Dwrite\s*\(", "write", "c"),
    (r"\bH5Dread\s*\(", "read", "c"), (r"\bMPI_File_open\s*\(", "open", "c"),
    (r"\bMPI_File_write\w*\s*\(", "write", "c"), (r"\bMPI_File_read\w*\s*\(", "read", "c"),
    (r"\bnc_create\w*\s*\(", "create", "c"), (r"\bnc_open\w*\s*\(", "open", "c"),
    (r"\bnc_put_var\w*\s*\(", "write", "c"), (r"\bnc_get_var\w*\s*\(", "read", "c"),
    (r"\bstd::ofstream\b|\bofstream\b", "write", "c"), (r"\bstd::ifstream\b|\bifstream\b", "read", "c"),
    (r"\badios2::\w+|\bDefineVariable\b", "write", "c"), (r"\bsqlite3_open\w*\s*\(", "db", "c"),
    (r"\bsqlite3_exec\s*\(", "db", "c"), (r"\bmkdir\s*\(", "create", "c"),
    (r"\bsilo|DBPutQuadmesh|DBCreate\b", "write", "c"), (r"\bvtk\w*Writer\b", "write", "c"),
    # Python
    (r"\bopen\s*\(", "open", "py"), (r"\bnp\.(save|savez\w*|savetxt|tofile)\b", "write", "py"),
    (r"\bnp\.(load|loadtxt|fromfile|genfromtxt)\b", "read", "py"),
    (r"\btorch\.save\b", "write", "py"), (r"\btorch\.load\b", "read", "py"),
    (r"\bh5py\.File\b", "open", "py"), (r"\bpd\.read_\w+", "read", "py"),
    (r"\.to_(csv|parquet|hdf|json|pickle|feather)\s*\(", "write", "py"),
    (r"\bjson\.dump\b", "write", "py"), (r"\bjson\.load\b", "read", "py"),
    (r"\bpickle\.dump\b", "write", "py"), (r"\bpickle\.load\b", "read", "py"),
    (r"\bsqlite3\.connect\b|\bcreate_engine\b|\bpymongo\b", "db", "py"),
    (r"\bxr\.open_\w+|\.to_netcdf\b|\bnetCDF4\.Dataset\b", "open", "py"),
    (r"\bPDBParser|MMCIFParser|PDBIO|MMCIFIO|gemmi\.|mdtraj\.|MDAnalysis", "open", "py"),
    (r"\bsave_pretrained\b|\bfrom_pretrained\b", "open", "py"),
    (r"\bsavefig\b|\bimwrite\b|\bimsave\b", "write", "py"),
    (r"\bshutil\.(copy\w*|move)\b|\bos\.(rename|replace)\b", "create", "py"),
    (r"\bsubprocess\.\w+\(|\bos\.system\(", "exec", "py"),
    # communicate: data moving between processes / nodes / services
    (r"\bMPI_(I?[sS]end|I?[rR]ecv|Sendrecv\w*|Put|Get|Accumulate)\s*\(", "communicate", "c"),
    (r"\bMPI_(I?(All)?[rR]educe\w*|I?Bcast|I?(All)?[gG]ather\w*|I?[sS]catter\w*|I?Alltoall\w*)\s*\(", "communicate", "c"),
    (r"\b(nccl|rccl)\w+\s*\(|\bsend\s*\(|\brecv\s*\(|\bcurl_easy_perform\b", "communicate", "c"),
    (r"\bcomm\.(send|recv|isend|irecv|bcast|gather|scatter|allreduce|reduce|allgather|alltoall|Send|Recv|Bcast|Gather|Scatter|Allreduce|Reduce|Allgather)\b", "communicate", "py"),
    (r"\bdist\.(all_reduce|broadcast|all_gather\w*|reduce_scatter\w*|send|recv|gather|scatter)\b", "communicate", "py"),
    (r"\brequests\.(get|post|put)\b|\bsocket\.|\bray\.(get|put)\b|\.remote\(|\bqueue\.(put|get)\b", "communicate", "py"),
    # compute: in-memory transformations of data (accelerator copies, model steps)
    (r"\b(cuda|hip)Memcpy\w*\s*\(|\bKokkos::deep_copy\b", "communicate", "c"),
    (r"\.(fit|predict|forward|backward|transform|fit_transform)\s*\(|\boptimizer\.step\b", "compute", "py"),
    (r"\.to\(\s*['\"]?(cuda|cpu|device)|\.cuda\(\)|\.cpu\(\)", "communicate", "py"),
]


def _sources(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            p = Path(dp) / f
            if p.suffix in PY_EXT or p.suffix in C_EXT:
                yield p


def scan(root, include="", limit=400):
    """Touch-points with the enclosing function, so an agent opens only the named lines."""
    inc = re.compile(include) if include else None
    compiled = [(re.compile(rx), kind, fam) for rx, kind, fam in PATTERNS]
    hits, kinds, n = [], Counter(), 0
    for p in _sources(root):
        rel = str(p.relative_to(root))
        if inc and not inc.search(rel):
            continue
        fam = "py" if p.suffix in PY_EXT else "c"
        n += 1
        func = None
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#") or (fam == "c" and s.startswith(("//", "/*", "*"))):
                continue
            m = FUNC[fam].match(line)
            if m and not s.endswith(";") and m.group(1) not in NOT_FUNCS:
                func = m.group(1)
            for rx, kind, f in compiled:
                if f == fam and rx.search(line):
                    kinds[kind] += 1
                    if len(hits) < limit:
                        hits.append({"file": rel, "line": i, "function": func, "kind": kind, "code": s[:160]})
                    break
    return {"files_scanned": n, "total_hits": sum(kinds.values()), "by_kind": dict(kinds), "top_files": Counter(h["file"] for h in hits).most_common(25),
            "hits": hits, "truncated": sum(kinds.values()) > len(hits)}


def inventory(root, newer_than=0.0, max_entries=200000):
    by_ext, n = {}, 0
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in {".git", "__pycache__"}]
        for f in fn:
            p = Path(dp) / f
            try:
                st = p.lstat()
            except OSError:
                continue
            if newer_than and st.st_mtime < newer_than:
                continue
            ext = p.suffix.lower() or "<none>"
            e = by_ext.setdefault(ext, {"count": 0, "bytes": 0, "examples": [], "hint": SCIENCE_EXT.get(ext, "")})
            e["count"] += 1
            e["bytes"] += st.st_size
            if len(e["examples"]) < 5:
                e["examples"].append(str(p.relative_to(root)))
            n += 1
            if n >= max_entries:
                break
    ranked = sorted(by_ext.items(), key=lambda kv: (kv[1]["hint"] == "", -kv[1]["bytes"]))
    return {"files": n, "truncated": n >= max_entries, "extensions": [{"ext": k, **v} for k, v in ranked], "likely_science": [k for k, v in ranked if v["hint"]]}


def register(mcp):
    @mcp.tool()
    def discover_inventory(pipeline: str, output_dir: str = "") -> dict:
        """Rank what the plain run wrote by likely scientific format and size. Use first in the discover stage. output_dir: where the data landed
        (default: where the smoke run ran). Returns {files, extensions: [{ext, count, bytes, examples, hint}], likely_science}."""
        c = P.load(pipeline)
        smoke = P.stage(c, "smoke")["outputs"]
        root = output_dir or smoke.get("cwd") or P.source_of(c)
        if not os.path.isdir(root):
            raise P.PipelineError(f"{root} is not a directory")
        out = inventory(root)
        P.record(pipeline, STAGE, status="running", outputs={"inventory": {k: out[k] for k in ("files", "likely_science")}, "output_dir": root}, note=f"inventory: {out['files']} files")
        return out

    @mcp.tool()
    def discover_scan(pipeline: str, include: str = "", limit: int = 400) -> dict:
        """Find where the application accesses, stores, communicates and computes data, with file, line and function. Use after discover_inventory, then
        read the writer of each artifact candidate and walk its callers. include: regex on file paths. Returns {by_kind, top_files, hits: [{file, line, function, kind, code}], truncated}."""
        out = scan(Path(P.source_of(P.load(pipeline))), include, limit)
        P.record(pipeline, STAGE, status="running", outputs={"scan": {k: out[k] for k in ("files_scanned", "total_hits", "by_kind")}}, note=f"scan: {out['total_hits']} touch-points")
        return out

    @mcp.tool()
    def discover_cases(query: str = "") -> dict:
        """Patterns from earlier workloads: artifact, entity types, id schemes, traps. Use before drafting candidates, to start from what is known. query: a domain word.
        Returns {cases: [text]}; seed entries are hypotheses to confirm."""
        parts = re.split(r"(?m)^## ", CASES.read_text())[1:] if CASES.exists() else []
        return {"cases": [p for p in parts if not query or query.lower() in p.lower()]}

    @mcp.tool()
    def discover_record(pipeline: str, artifacts: list, questions: list) -> dict:
        """Save the artifact candidates and the questions for the user. Use when the evidence is in; it writes <pipeline>/discovery.md for the gate. artifacts: [{name, evidence, writer,
        upstream, downstream}]. questions: what only the user can settle (which artifacts, granularity, exclusions). Returns {status, discovery}."""
        artifacts = json.loads(artifacts) if isinstance(artifacts, str) else artifacts
        questions = json.loads(questions) if isinstance(questions, str) else questions
        bad = [a.get("name", "?") for a in artifacts if not (a.get("name") and a.get("evidence") and a.get("writer"))]
        if bad:
            return {"status": "error", "error": f"each candidate needs name, evidence and writer: {bad}"}
        lines = ["# Provenance discovery", ""]
        for a in artifacts:
            lines += [f"## {a['name']}", f"- evidence: {a['evidence']}", f"- writer: {a['writer']}", f"- upstream: {a.get('upstream', '')}", f"- downstream: {a.get('downstream', '')}", ""]
        lines += ["## Questions", ""] + [f"- {q}" for q in questions]
        f = P.work(P.load(pipeline)) / "discovery.md"
        f.write_text("\n".join(lines) + "\n")
        P.record(pipeline, STAGE, outputs={"artifacts": artifacts, "questions": questions, "discovery": str(f), "summary": f"{len(artifacts)} artifact candidate(s), {len(questions)} question(s)"})
        return {"status": "ok", "discovery": str(f)}

    @mcp.tool()
    def discover_validate(pipeline: str) -> dict:
        """Validate the discover stage: outputs were inventoried, the code was scanned, each artifact candidate names its evidence and writer, and there are questions for the user.
        Use before the gate. Returns the verdict {status, failed, unknown, passed, total}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        v.check("inventory", bool(o.get("inventory")), "discover_inventory has not run", "run discover_inventory")
        v.check("scan", bool(o.get("scan")), "discover_scan has not run", "run discover_scan")
        if o.get("scan"):
            v.check("touch_points", o["scan"]["total_hits"] > 0, str(o["scan"]), "nothing found: is the source right? Does the app do its I/O through a library the patterns miss?")
        v.check("candidates", bool(o.get("artifacts")), "no artifact candidate", "name 1 to 3 candidates with discover_record")
        v.check("questions", bool(o.get("questions")), "no question for the user", "the user decides the artifacts and granularity: ask")
        return P.finish_validation(pipeline, v)
