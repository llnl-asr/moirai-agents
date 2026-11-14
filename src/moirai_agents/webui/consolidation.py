"""The web server's consolidation report, kept off the request path.

consolidate.report() compares every capability's rules with every other capability's rules. Over the ~160
capabilities of all layers that is close to a minute of pure Python. Run in a server thread, it holds the GIL, and
every request that does file I/O waits for the GIL again after each system call: for the first minute after a start,
a capability listing took 15-25 s. So:

- the capabilities part runs in a child process (``python -m moirai_agents.webui.consolidation``). The server only
  waits on a pipe, which does not hold the GIL;
- it runs only when a capability's SKILL.md or capability.json changed since the last report. The result is kept in
  <state>/consolidation.json with that fingerprint, so a restart with an unchanged store does not recompute it;
- the lessons part is cheap (a few hundred lessons), so it is recomputed in-process every time a fresh report is needed.

Until the first capabilities report exists, the report has no capability groups and ``capabilities_pending`` is true.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import capstore, consolidate

CHILD_TIMEOUT_S = float(os.environ.get("MOIRAI_AGENTS_CONSOLIDATION_TIMEOUT_S", "1800"))


def caps_fingerprint() -> str:
    """Changes when a capability is added, removed, moved to another layer, or has its SKILL.md or capability.json changed."""
    h = hashlib.sha1()
    for name, (layer, d, _meta) in sorted(capstore.all_caps().items()):
        h.update(f"{name}\0{layer}\0{d}".encode())
        for f in ("SKILL.md", "capability.json"):
            try:
                st = (d / f).stat()
                h.update(f"\0{st.st_mtime_ns}:{st.st_size}".encode())
            except OSError:
                h.update(b"\0-")
    return h.hexdigest()


class Consolidation:
    """One server's consolidation report: the capabilities part from a child process, the lessons part in-process."""

    def __init__(self, state: Path) -> None:
        self.f = Path(state) / "consolidation.json"
        self.lock = threading.Lock()          # one child at a time
        try:
            saved = json.loads(self.f.read_text())
            self.caps: Optional[List[Dict[str, Any]]] = saved["capabilities"]
            self.fp, self.checked = saved["fp"], saved.get("checked", 0.0)
        except (OSError, ValueError, KeyError, TypeError):
            self.caps, self.fp, self.checked = None, "", 0.0

    def _save(self) -> None:
        tmp = self.f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fp": self.fp, "checked": self.checked, "capabilities": self.caps}))
        os.replace(tmp, self.f)

    def refresh_capabilities(self) -> bool:
        """Recompute the capabilities part in a child process when the store changed. Returns whether it was recomputed."""
        with self.lock:
            fp = caps_fingerprint()
            if self.caps is not None and fp == self.fp:
                return False
            p = subprocess.run([sys.executable, "-m", __name__], capture_output=True, text=True, timeout=CHILD_TIMEOUT_S,
                               stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise RuntimeError(f"capabilities consolidation check exited {p.returncode}: {p.stderr.strip()[-500:]}")
            self.caps, self.fp, self.checked = json.loads(p.stdout), fp, round(time.time(), 1)
            self._save()
            return True

    def report(self) -> Dict[str, Any]:
        """The report now: fresh lessons groups, the last capabilities groups, without what was kept, asked or resolved since."""
        rep = {"lessons": consolidate.lessons(), "capabilities": list(self.caps or []), "checked": round(time.time(), 1),
               "capabilities_checked": self.checked, "capabilities_pending": self.caps is None}
        return consolidate.unkept(rep)


def main() -> int:
    json.dump(consolidate.capabilities(), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
