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
LESSON_SKILLS = ("dftracer-lessons", "dftracer-pitfalls", "dftracer-annotation-lessons")     # dftracer-agents' skills that are all lessons
LESSON_SECTION = re.compile(r"lesson|pitfall|known issue|gotcha|corner case", re.I)
LESSON_RX = re.compile(r"^- \[(L-[0-9a-f]+)\] (.*?)(?=^- \[L-|\Z)", re.M | re.S)
LESSON_TITLE_CHARS = 120
LESSON_QUERY_CHARS = 400
LESSON_BUDGET = 2000
SKILL_FAMILIES = ("workload-", "software-", "system-", "tools-", "flux-", "slurm-", *LESSON_SKILLS)   # what dftracer-agents has learnt
MEMORY_TYPES = ("user", "feedback", "project", "reference")
NODE_RX = re.compile(r"^NODE (.+?) \[src=(\S+) loc=L(\d+)")
SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
QUERY_S, BUILD_S = 60, 300
DEFAULT_BUDGET = 1200
LESSON_MIN_WORDS = 2
STOP_WORDS = frozenset("the and for with from that this not was were are but has have into when failed fails error".split())
_nodes_cache: Dict[str, Any] = {}
FAILED = ("error", "fail", "failed")
FLOW_TOOLS = ("pipeline_", "plan_", "lesson_", "knowledge_", "memory_", "capability_", "ask_user")    # a refusal here is the flow working, not a failure
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


def _lesson_headings(text: str) -> str:
    """LESSONS.md with each lesson a heading: graphify indexes headings, so a bullet would never be found."""
    rows = [f"## [{lid}] {' '.join(body.split())[:LESSON_TITLE_CHARS]}\n\n{body.strip()}\n" for lid, body in LESSON_RX.findall(text)]
    return "\n".join([text.split("\n", 1)[0] + "\n", *rows])


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
        if f.name == "LESSONS.md":
            (stage / rel).write_text(_lesson_headings(f.read_text(errors="replace")))
        else:
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


def _is_lesson(source: str, line: int) -> bool:
    """Is this graph hit a lesson: in LESSONS.md, in a skill that is all lessons, or under a section of lessons in a skill."""
    if not source.endswith(".md") or source.startswith(("memory/", "dftracer-memory/")):
        return False
    if source.endswith("LESSONS.md"):
        return line > 1
    if Path(source).parent.name in LESSON_SKILLS:
        return True
    try:
        head = (_root() / "stage" / source).read_text(errors="replace").splitlines()[:line]
    except OSError:
        return False
    section = next((x for x in reversed(head) if x.startswith("## ")), "")
    return bool(LESSON_SECTION.search(section)) or bool(LESSON_SECTION.search(head[-1] if head else ""))


def _lesson_nodes(graph: str) -> List[Dict[str, Any]]:
    """Every lesson node of the graph. A query only walks out from a few seeds, so a specific lesson among thousands of nodes needs ranking on its own."""
    state = (_root() / "state.json").read_text()
    if _nodes_cache.get("state") != state:
        rows = []
        for n in json.loads(Path(graph).read_text()).get("nodes", []):
            line = int(str(n.get("source_location") or "L0").lstrip("L") or 0)
            if n.get("file_type") == "document" and _is_lesson(n.get("source_file", ""), line):
                rows.append({"title": n.get("label", ""), "source": n["source_file"], "line": line})
        _nodes_cache.update(state=state, rows=rows)
    return _nodes_cache["rows"]


def _ranked_lessons(text: str, graph: str) -> List[Dict[str, Any]]:
    words = {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower()) if w not in STOP_WORDS}
    need = min(LESSON_MIN_WORDS, len(words))
    scored = [(sum(w in r["title"].lower() for w in words), r) for r in _lesson_nodes(graph)]
    return [r for n, r in sorted(scored, key=lambda x: -x[0]) if n and n >= need]


def lessons(failure: str, k: int = 6) -> Dict[str, Any]:
    """The lessons closest to a failure: every lesson in the graph ranked on its words, the graph's walk from them, then lessons on record."""
    text = " ".join((failure or "").split())[:LESSON_QUERY_CHARS]
    g = _graph_hits(text, LESSON_BUDGET)
    walked = [h for h in g["hits"] if _is_lesson(h["source"], h["line"])]
    ranked = _ranked_lessons(text, ensure()["graph"]) if not g.get("error") else []
    rows: List[Dict[str, Any]] = [{"kind": "lesson", **h} for h in {(h["source"], h["line"]): h for h in ranked + walked}.values()]
    seen = {h["title"].split("]")[0].lstrip("[") for h in rows}
    rows += [r for r in learning.search(text, k) if r["kind"] == "lesson" and r["id"] not in seen]
    out = {"status": "ok", "results": rows[:k]}
    if g.get("error"):
        out["graph_error"] = g["error"]
    return out


def _failure_text(tool: str, res: Dict[str, Any]) -> str:
    parts = [tool.replace("_", " "), str(res.get("error") or "")]
    parts += [f"{f.get('name', '')} {f.get('detail', '')}" for f in res.get("failed") or [] if isinstance(f, dict)]
    return " ".join(parts + [str(res.get("log_tail") or "")[-LESSON_QUERY_CHARS // 2:]])


def on_failure(tool: str, res: Any) -> Any:
    """A failed tool's result, with the lessons closest to it, so what is known is read before anything is explored."""
    failed = isinstance(res, dict) and (res.get("status") in FAILED or res.get("rc") not in (None, 0))
    if not failed or tool.startswith(FLOW_TOOLS):
        return res
    try:
        rows = lessons(_failure_text(tool, res))["results"]
    except Exception:
        return res
    if rows:
        res = {**res, "known_lessons": rows, "before_exploring": "Read these first, with knowledge_read(source, line) for a graph row: one may already be the fix."}
    return res


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
