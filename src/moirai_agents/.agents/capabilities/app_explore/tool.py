"""Run pipeline stage 1: explore the system, then the application, before anything is changed."""
import ast
import os
import platform
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from moirai_agents.core import dfbridge as B
from moirai_agents.core import moirai_env as M
from moirai_agents.core import pipeline as P

STAGE = "explore"
LANG = {".py": "python", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".f90": "fortran", ".f": "fortran"}
BUILD = {"CMakeLists.txt": "cmake", "configure.ac": "autotools", "configure": "autotools", "meson.build": "meson", "Makefile": "make",
         "makefile": "make", "pyproject.toml": "pip", "setup.py": "pip"}
SKIP = {".git", "__pycache__", "build", "dist", ".venv", "venv", "node_modules"}


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip().splitlines()[0][:160] if (r.stdout or r.stderr).strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _system():
    shm = shutil.disk_usage("/dev/shm") if os.path.isdir("/dev/shm") else None
    sched = "flux" if shutil.which("flux") else "slurm" if shutil.which("sbatch") else "none"
    return {"host": platform.node().split(".")[0], "cores": os.cpu_count(), "scheduler": sched, "in_allocation": bool(os.environ.get("FLUX_URI") or os.environ.get("SLURM_JOB_ID")),
            "compilers": {c: _run([c, "--version"]) for c in ("gcc", "g++", "clang", "cc") if shutil.which(c)},
            "python": platform.python_version(), "dev_shm_free_mib": shm.free >> 20 if shm else None, "moirai": M.locate()}


def _app(src):
    root = Path(src).expanduser()
    if not root.is_dir():
        raise P.PipelineError(f"{src} is not a directory")
    langs, entries, builds, n = Counter(), [], {}, 0
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP]
        for f in fn:
            n += 1
            p = Path(dp) / f
            rel = str(p.relative_to(root))
            lang = LANG.get(p.suffix.lower())
            if lang:
                langs[lang] += 1
            if f in BUILD and p.parent == root:
                builds[f] = BUILD[f]
            if p.suffix == ".py" and p.stat().st_size < 2 << 20:
                try:
                    txt = p.read_text(errors="replace")
                    if "__main__" in txt and any(isinstance(x, ast.If) and "__main__" in ast.dump(x.test) for x in ast.parse(txt).body):
                        entries.append(rel)
                except (SyntaxError, ValueError):
                    pass
            elif lang in ("c", "cpp") and p.suffix != ".h" and p.stat().st_size < 2 << 20:
                if re.search(r"^\s*int\s+main\s*\(", p.read_text(errors="replace"), re.M):
                    entries.append(rel)
    git = _run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
    return {"root": str(root), "files": n, "languages": dict(langs.most_common()), "build_files": builds, "entry_points": entries[:40], "git_head": git}


def register(mcp):
    @mcp.tool()
    def explore_run(pipeline: str) -> dict:
        """Explore this system (scheduler, compilers, /dev/shm, where moirai is) and the application (languages, build system, entry points).
        Use first in a run pipeline; it changes nothing. Returns {system, app}."""
        c = P.load(pipeline)
        P.record(pipeline, STAGE, status="running", note="exploring system and application")
        out = {"system": _system(), "app": _app(P.source_of(c))}
        if c.get("session"):             # the session's own view: build tool, features
            out["session_detect"] = B.call("session_detect", {"run_id": c["session"]["run_id"]})
        if B.available():                # dftracer-agents knows the site: modules, launcher, file systems
            out["system"]["dftracer_detect"] = B.call("system_detect")
        s = f"{'/'.join(out['app']['languages']) or 'no source'}, build: {'/'.join(sorted(set(out['app']['build_files'].values()))) or 'none'}, " \
            f"{len(out['app']['entry_points'])} entry point(s), scheduler {out['system']['scheduler']}"
        P.record(pipeline, STAGE, outputs={**out, "summary": s})
        return out

    @mcp.tool()
    def explore_validate(pipeline: str) -> dict:
        """Validate the explore stage: moirai is usable, the app has a supported language and an entry point, /dev/shm can hold a ring.
        Use after explore_run. Returns the verdict {status, failed, unknown, passed, total}."""
        o = P.stage(P.load(pipeline), STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if v.check("explored", bool(o.get("app")), "explore_run has not run", "run explore_run"):
            m = o["system"]["moirai"]
            v.check("moirai", m.get("ok"), m.get("error", "moirai found"), m.get("fix", ""))
            v.check("dftracer_utils", m.get("dftracer_utils"), "dftracer-utils is needed for the query stage", "pip install --pre dftracer-utils into the moirai venv")
            v.check("language", bool(set(o["app"]["languages"]) & {"python", "c", "cpp"}), str(o["app"]["languages"]), "moirai annotates Python, C and C++")
            v.check("entry_point", bool(o["app"]["entry_points"]), f"{len(o['app']['entry_points'])} found", "ask the user which file starts the app")
            shm = o["system"].get("dev_shm_free_mib")
            if shm is None:
                v.inconclusive("dev_shm", "no /dev/shm on this host")
            else:
                v.check("dev_shm", shm >= 32, f"{shm} MiB free", "free /dev/shm; the run uses a 16 MiB ring")
        return P.finish_validation(pipeline, v)
