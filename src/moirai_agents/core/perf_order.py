"""The dftracer-agents annotation, in moirai's style: import dftracer's annotations from moirai.

dftracer-agents' python annotators write `from dftracer.python import dftracer, dft_fn as DFTracerFn` into every file. pydftracer decides at its own
import whether it is enabled (DFTRACER_ENABLE, DFTRACER_INIT, DFTRACER_ATTACH), and importing moirai sets those and then imports pydftracer
(moirai python/moirai/__init__.py), exposing dft_fn, dftracer and ai. So each annotated file gets `from moirai import ...` in place of that import:
nothing imports pydftracer before moirai has set its environment, and the application imports only moirai.
"""
import re
from pathlib import Path
from typing import Iterable, List

_DFT = re.compile(r"^from dftracer\.python import (?P<names>.+)$")
_OLD_MARK = re.compile(r"^import _moirai_prov  # noqa: F401  moirai sets DFTRACER_\* before dftracer is imported$")


def use_moirai(text: str) -> str:
    """The text with every top-level `from dftracer.python import X` read as `from moirai import X`; a line added by the earlier ordering step is dropped."""
    out = []
    for line in text.split("\n"):
        if _OLD_MARK.match(line):
            continue
        m = _DFT.match(line)
        out.append(f"from moirai import {m.group('names')}" if m else line)
    return "\n".join(out)


def use_moirai_files(paths: Iterable[Path]) -> List[str]:
    """Apply use_moirai to each file; returns the paths changed. The provenance helper is never touched."""
    changed = []
    for p in map(Path, paths):
        if p.name == "_moirai_prov.py":
            continue
        old = p.read_text()
        new = use_moirai(old)
        if new != old:
            p.write_text(new)
            changed.append(str(p))
    return changed
