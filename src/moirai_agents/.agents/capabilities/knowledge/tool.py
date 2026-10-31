"""Find what is known: capabilities, lessons, memory and dftracer-agents' learnt skills, through one graph."""
from moirai_agents.core import capstore as C
from moirai_agents.core import knowledge as K
from moirai_agents.core import pipeline as P


def register(mcp):
    @mcp.tool()
    def knowledge_search(question: str, k: int = 8, budget: int = 1200) -> dict:
        """The first place to look: before anything unfamiliar, and to pick a capability, a skill, a past fix or a fact. It walks the knowledge graph over capabilities,
        lessons, memory and dftracer-agents' workload, software, system and tools skills, then reads lessons and memory directly. Name the error, tool or app in the question.
        k: rows per kind. budget: graph tokens to spend. Returns {status, graph: [{title, source, line}], lessons, memory, graph_error?}; open a graph row with knowledge_read."""
        return K.search(question, k, budget)

    @mcp.tool()
    def knowledge_read(source: str, line: int = 1, lines: int = 60) -> dict:
        """Read one section that knowledge_search named, instead of the whole file. source, line: as returned; lines: the most to read.
        Returns {status, source, line, text}."""
        return K.read(source, line, lines)

    @mcp.tool()
    def memory_write(name: str, description: str, body: str, type: str) -> dict:
        """Remember what is true here: a decision, a constraint, a user's guidance, a pointer. How to do something belongs in a capability; a verified fix in a lesson.
        name: kebab-case slug, the same name updates. description: one line with the words someone would search. body: the fact, with [[name]] links to related memories.
        type: user|feedback|project|reference; feedback needs **Why:** and **How to apply:** lines. Returns {status, name}."""
        return K.remember(name, description, body, type)

    @mcp.tool()
    def knowledge_validate() -> dict:
        """Rule on the graph: it builds, and every capability and memory is in it. Use after recording something, or when knowledge_search reports graph_error.
        Returns {status, failed, passed, total}."""
        v = P.Verdict("knowledge")
        g = K.ensure()
        v.check("graph", g["status"] == "ok", g.get("error", ""), "run scripts/setup_env.sh to install graphify, then knowledge_validate again")
        if g["status"] == "ok":
            stage = K._root() / "stage"
            for n in C.all_caps():
                v.check(f"capability:{n}", (stage / "capabilities" / n).is_dir(), n, "rebuild: the graph misses this capability")
            for f in K.memory_dir().glob("*.md"):
                v.check(f"memory:{f.stem}", (stage / "memory" / f.name).exists(), f.stem, "rebuild: the graph misses this memory")
        return v.result()
