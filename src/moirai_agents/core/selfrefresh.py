"""Self-refresh: edits take effect without anyone restarting anything.

:class:`Watcher` hashes source files. A changed file is byte-compiled first: a broken edit is reported and the running
code stays. Each long-running process decides what a valid change means for it (see ``mcp_server`` and ``webui.server``).
"""
from __future__ import annotations

import py_compile
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def mtimes(paths: Iterable[Path]) -> Dict[str, int]:
    out = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime_ns
        except OSError:
            pass
    return out


def broken(files: Iterable[str]) -> List[str]:
    """The .py files that do not compile, with the error."""
    bad = []
    for f in files:
        if not f.endswith(".py"):
            continue
        with tempfile.NamedTemporaryFile(suffix=".pyc") as tmp:
            try:
                py_compile.compile(f, cfile=tmp.name, doraise=True)
            except (py_compile.PyCompileError, OSError) as e:
                bad.append(f"{Path(f).name}: {str(e).strip().splitlines()[-1][:200]}")
    return bad


class Watcher:
    def __init__(self, paths_fn) -> None:
        self.paths_fn = paths_fn
        self.seen = mtimes(paths_fn())
        self.error = ""

    def changes(self) -> Tuple[List[str], str]:
        """(changed files, compile error). Changes are only consumed once they compile."""
        cur = mtimes(self.paths_fn())
        changed = sorted(p for p, m in cur.items() if self.seen.get(p) != m)
        if not changed:
            return [], self.error
        bad = broken(changed)
        if bad:
            self.error = "; ".join(bad)
            return [], self.error
        self.seen, self.error = cur, ""
        return changed, ""
