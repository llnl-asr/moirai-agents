"""Finding what is known. One graphify graph over every capability, lesson, memory and dftracer-agents' learnt skills is the first place to look;
direct reads of lessons and memory cover what the graph cannot, since graphify matches headings and symbols, not body text. Stdlib only: without
graphify or dftracer-agents the search still answers from the direct reads, and says so.

    <learning>/graph/stage/capabilities/<name>/{SKILL.md,LESSONS.md,tool.py}   copied, so a graph node's path stays valid and readable
    <learning>/graph/stage/memory/<name>.md
    <learning>/graph/stage/dftracer/<skill>/SKILL.md
    <learning>/graph/stage/dftracer-memory/<name>.md                           what dftracer-agents remembers
    <learning>/memory/<name>.md                                                the memory store, one fact per file
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import capstore, learning

CAP_FILES = ("SKILL.md", "LESSONS.md", "tool.py")
SKILL_FAMILIES = ("workload-", "software-", "system-", "tools-", "flux-", "slurm-")   # what dftracer-agents has learnt
MEMORY_TYPES = ("user", "feedback", "project", "reference")
NODE_RX = re.compile(r"^NODE (.+?) \[src=(\S+) loc=L(\d+)")
SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
QUERY_S, BUILD_S = 60, 300
DEFAULT_BUDGET = 1200
PACK_ROWS, PACK_BUDGET, PACK_ROW_CHARS = 6, 600, 240


def _root() -> Path:
    return capstore._learning_dir() / "graph"


def memory_dir() -> Path:
    d = capstore._learning_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dftracer_skills() -> Optional[Path]:
    try:
        import dftracer_agents
    except ImportError:
        return None
    d = Path(dftracer_agents.__file__).parent / ".agents" / "skills"
    return d if d.is_dir() else None


def _sources() -> Iterator[Tuple[str, Path]]:
    """(path in the stage, file): what the graph is built from."""
    for name, (_, d, _m) in sorted(capstore.all_caps().items()):
        for f in CAP_FILES:
            if (d / f).exists():
                yield f"capabilities/{name}/{f}", d / f
    for f in sorted(memory_dir().glob("*.md")):
        yield f"memory/{f.name}", f
    skills = _dftracer_skills()
    if skills:
        for f in sorted(skills.glob("*/SKILL.md")):
            if f.parent.name.startswith(SKILL_FAMILIES):
                yield f"dftracer/{f.parent.name}/SKILL.md", f
    theirs = _dftracer_memory()
    if theirs:
        for f in sorted(theirs.glob("*.md")):
            yield f"dftracer-memory/{f.name}", f


def _dftracer_memory() -> Optional[Path]:
    try:
        from dftracer_agents.bootstrap import bundled_memory_dir
    except ImportError:
        return None
    d = Path(bundled_memory_dir())
    return d if d.is_dir() else None


def _graphify() -> Optional[str]:
    return shutil.which("graphify", path=str(Path(sys.executable).parent)) or shutil.which("graphify")


def ensure(force: bool = False) -> Dict[str, Any]:
    """Rebuild the graph if what it covers changed: hashed on content, so a checkout that only touches mtimes costs nothing."""
    exe = _graphify()
    if not exe:
        return {"status": "error", "error": "graphify is not installed: run scripts/setup_env.sh"}
    src = list(_sources())
    h = hashlib.sha256()
    for rel, f in src:
        h.update(rel.encode())
        h.update(f.read_bytes())
    digest, root = h.hexdigest(), _root()
    stage, state = root / "stage", root / "state.json"
    graph = stage / "graphify-out" / "graph.json"
    try:
        old = json.loads(state.read_text())
    except (OSError, ValueError):
        old = {}
    if not force and graph.exists() and old.get("hash") == digest:
        return {"status": "ok", "rebuilt": False, "graph": str(graph), "nodes": old.get("nodes", 0), "files": len(src)}
    shutil.rmtree(stage, ignore_errors=True)
    for rel, f in src:
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, stage / rel)
    r = subprocess.run([exe, "update", str(stage), "--no-cluster"], capture_output=True, text=True, timeout=BUILD_S, cwd=stage)
    if not graph.exists():
        return {"status": "error", "error": f"graphify built no graph: {(r.stderr or r.stdout)[-300:]}"}
    nodes = len(json.loads(graph.read_text()).get("nodes", []))
    state.write_text(json.dumps({"hash": digest, "nodes": nodes, "ts": round(time.time(), 1)}))
    return {"status": "ok", "rebuilt": True, "graph": str(graph), "nodes": nodes, "files": len(src)}


def _graph_hits(question: str, budget: int) -> Dict[str, Any]:
    g = ensure()
    if g["status"] != "ok":
        return {"hits": [], "error": g["error"]}
    stage = str(_root() / "stage") + "/"
    try:
        r = subprocess.run([_graphify(), "query", question, "--budget", str(budget), "--graph", g["graph"]], capture_output=True, text=True, timeout=QUERY_S)
    except subprocess.TimeoutExpired:
        return {"hits": [], "error": "graphify timed out: narrow the question or lower budget"}
    hits = []
    for line in r.stdout.splitlines():
        m = NODE_RX.match(line)
        if m:
            src = m.group(2).replace(stage, "")
            hits.append({"title": m.group(1), "source": src, "line": int(m.group(3))})
    return {"hits": hits}


def search_memory(query: str, k: int = 5) -> List[dict]:
    """Rank this store's memories and dftracer-agents' by their text, following one hop of [[links]]."""
    try:
        from dftracer_agents.memory_graph import load_memories, recall
    except ImportError:
        words = set(re.findall(r"[a-z0-9_]{3,}", query.lower()))
        rows = [(sum(w in f.read_text().lower() for w in words), f) for f in memory_dir().glob("*.md")]
        return [{"name": f.stem, "score": s, "source": f"memory/{f.name}"} for s, f in sorted(rows, key=lambda x: -x[0])[:k] if s]
    mems = [{**m, "source": f"memory/{m['file']}"} for m in load_memories(memory_dir())]
    theirs = _dftracer_memory()
    if theirs:
        mems += [{**m, "source": f"dftracer-memory/{m['file']}"} for m in load_memories(theirs)]
    where = {m["name"]: m["source"] for m in mems}
    return [{**{x: m.get(x) for x in ("name", "description", "type", "score", "via")}, "source": where[m["name"]]} for m in recall(query, k=k, memories=mems)]


def search(question: str, k: int = 8, budget: int = DEFAULT_BUDGET) -> Dict[str, Any]:
    """The graph first, then the direct reads. `graph_error` says when the graph could not answer."""
    g = _graph_hits(question, budget)
    out = {"status": "ok", "graph": g["hits"][:k * 2], "lessons": learning.search(question, k), "memory": search_memory(question, k)}
    if g.get("error"):
        out["graph_error"] = g["error"]
    return out


def read(source: str, line: int = 1, lines: int = 60) -> Dict[str, Any]:
    """A section of a file search named, from its line up to the next heading of the same depth."""
    root = (_root() / "stage").resolve()
    f = (root / source).resolve()
    if not f.is_relative_to(root) or not f.is_file():
        return {"status": "error", "error": f"{source} is not in the graph: use a source knowledge_search returned"}
    text = f.read_text(errors="replace").splitlines()
    start = max(line - 1, 0)
    head = re.match(r"#+ ", text[start]) if start < len(text) else None
    end = min(start + lines, len(text))
    if head and f.suffix == ".md":
        depth = head.group(0)
        stop = next((i for i in range(start + 1, end) if re.match(rf"#{{1,{len(depth) - 1}}} ", text[i])), end)
        end = stop
    return {"status": "ok", "source": source, "line": start + 1, "text": "\n".join(text[start:end])}


def remember(name: str, description: str, body: str, kind: str) -> Dict[str, Any]:
    """Write one memory; writing the same name again updates it."""
    if not SLUG_RX.match(name or ""):
        return {"status": "error", "error": "name must be a short kebab-case slug"}
    if kind not in MEMORY_TYPES:
        return {"status": "error", "error": f"type must be one of {MEMORY_TYPES}"}
    if not (description or "").strip() or not (body or "").strip():
        return {"status": "error", "error": "a memory needs a description and a body"}
    if kind == "feedback" and not ("**Why:**" in body and "**How to apply:**" in body):
        return {"status": "error", "error": "a feedback memory needs **Why:** and **How to apply:** lines"}
    (memory_dir() / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {' '.join(description.split())}\nmetadata:\n  type: {kind}\n---\n\n{body.strip()}\n")
    return {"status": "ok", "name": name}


def _seen(session: str) -> Tuple[Path, set]:
    d = _root() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    f = d / (hashlib.sha256(session.encode()).hexdigest()[:16] + ".json")
    try:
        return f, set(json.loads(f.read_text()))
    except (OSError, ValueError):
        return f, set()


def pack(prompt: str, session: str = "hook", limit: int = PACK_ROWS) -> str:
    """What a turn should know before it starts: the best rows it has not been shown in this session, or "" when there is nothing new."""
    r = search(prompt, limit, PACK_BUDGET)
    rows = [(f"{h['source']}:{h['line']}", f"{h['title']} ({h['source']}:{h['line']})") for h in r["graph"]]
    rows += [(f"lesson:{x['id']}", f"lesson {x['id']} on {x['capability']}: {x['text']}") for x in r["lessons"] if x["kind"] == "lesson"]
    rows += [(m["source"], f"memory {m['name']}: {m.get('description') or ''} ({m['source']})") for m in r["memory"]]
    f, seen = _seen(session)
    fresh = [(key, text) for key, text in rows if key not in seen][:limit]
    if not fresh:
        return ""
    f.write_text(json.dumps(sorted(seen | {key for key, _ in fresh})))
    body = "\n".join(f"- {text[:PACK_ROW_CHARS]}" for _, text in fresh)
    return f"<moirai-context>\nPossibly relevant, found by knowledge_search; open a graph row with knowledge_read(source, line):\n{body}\n</moirai-context>"
