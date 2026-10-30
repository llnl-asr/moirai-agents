"""The capability store. A capability is one directory, and everything in the framework is one:

    <name>/capability.json   name, description, version, stages, enabled, history, and ``pvc``: which tools predict and which validate
    <name>/tool.py           the MCP tools: ``register(mcp)``; predictors, and validators that rule deterministically on what they made
    <name>/SKILL.md          rules, constraints, corner cases, the Corrector (how a failed verdict repairs the original tool), self-heal and self-learn
    <name>/LESSONS.md        what use taught it
    <name>/agent.json        the background agent: its fixed scope (tools, skills, permissions, level)
    <name>/checks.json       known input -> expected output, run in a sandbox on every change
    <name>/versions/v<N>/    every earlier version, so rollback is one call

Layers (a higher one shadows a lower one wholesale): bundled -> user -> project (``capabilities/`` of the repo: committed, shared). Changes follow the
predictor/validator discipline: propose (static checks, duplicate fingerprint, bundle checks, sandboxed checks),
then a person approves, then it is live without a restart. Nothing runs in the server before approval.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import typing
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PKG = Path(__file__).resolve().parents[1]
BUNDLED = PKG / ".agents" / "capabilities"
NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execvp"), ("os", "spawnl")}
FORBIDDEN_IMPORTS = {"ctypes", "pty", "telnetlib", "ftplib", "paramiko"}
REVIEW_IMPORTS = {"socket": "opens network connections", "requests": "makes HTTP requests", "urllib": "makes HTTP requests",
                  "http": "makes HTTP requests", "subprocess": "runs commands", "shutil": "copies/removes files"}
ABS_PATH = re.compile(r"(?:/usr/WS\d|/usr/workspace|/g/g\d+|/home/[a-z]|/p/(?:lustre|vast|gpfs)\d?/[a-z]|/Users/)")
SKILL_SECTIONS = ("Rules", "Constraints", "Corner cases", "Corrector", "Self-heal", "Self-learn")
BUILTIN_AGENT_TOOLS = {"Read", "Grep", "Glob", "Bash", "Edit", "Write", "Task", "WebFetch", "WebSearch", "TodoWrite"}
SHARED_AGENT_TOOLS = ["pipeline_status", "pipeline_feedback", "ask_user", "lesson_add", "lesson_search"]
LEVELS = ("level_1", "level_2", "level_3")


class _State:
    loaded: Dict[str, dict] = {}
    fingerprint = ""
    notify = False


def layers() -> List[Tuple[str, Path]]:
    """Lowest precedence first."""
    return [("bundled", Path(os.environ.get("MOIRAI_AGENTS_BUNDLED_CAPABILITIES", BUNDLED))),
            ("user", Path(os.environ.get("MOIRAI_AGENTS_USER_CAPABILITIES", "~/.moirai-agents/capabilities")).expanduser()),
            ("project", Path(os.environ.get("MOIRAI_AGENTS_PROJECT_CAPABILITIES", PKG.parents[1] / "capabilities")).expanduser())]


def layer_dir(layer: str) -> Path:
    return dict(layers())[layer]


def all_caps() -> Dict[str, Tuple[str, Path, dict]]:
    """name -> (layer, dir, meta), higher layers shadowing lower ones wholesale."""
    out: Dict[str, Tuple[str, Path, dict]] = {}
    for layer, root in layers():
        for m in sorted(root.glob("*/capability.json")) if root.exists() else []:
            try:
                meta = json.loads(m.read_text())
            except ValueError:
                continue
            out[m.parent.name] = (layer, m.parent, meta)
    return out


def _err(msg: str, **extra) -> dict:
    return {"status": "error", "error": msg, **extra}


# ------------------------------------------------------------------------------- tool shim (the `mcp` argument)
_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _schema_for(fn: Callable) -> dict:
    props, required = {}, []
    hints = typing.get_type_hints(fn) if fn.__annotations__ else {}
    for pname, p in inspect.signature(fn).parameters.items():
        ann = hints.get(pname, str)
        if typing.get_origin(ann) is typing.Union:
            args = [a for a in typing.get_args(ann) if a is not type(None)]
            ann = args[0] if args else str
        ann = typing.get_origin(ann) or ann
        props[pname] = {"type": _TYPES.get(ann, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    return {"type": "object", "properties": props, "required": required}


class ToolShim:
    """Looks like FastMCP's ``mcp`` object: ``@mcp.tool()``."""

    def __init__(self, owner: str = "") -> None:
        self.tools: Dict[str, dict] = {}
        self.owner = owner

    def tool(self, fn: Optional[Callable] = None, name: Optional[str] = None, description: Optional[str] = None):
        def deco(f: Callable):
            n = name or f.__name__
            self.tools[n] = {"fn": f, "capability": self.owner,
                             "schema": {"name": n, "description": description or (inspect.getdoc(f) or ""), "inputSchema": _schema_for(f)}}
            return f
        return deco(fn) if callable(fn) else deco


# ------------------------------------------------------------------------------- validators
def fingerprint(code: str) -> str:
    """Hash of the code without docstrings, comments and function names: a twin under another name has the same one."""
    tree = ast.parse(code)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) and isinstance(n.body[0].value.value, str):
                n.body = n.body[1:] or [ast.Pass()]
            if hasattr(n, "name"):
                n.name = "_"
    return hashlib.sha1(ast.dump(tree).encode()).hexdigest()[:16]


def analyze(code: str, taken_tools: Optional[Dict[str, str]] = None, own: str = "") -> dict:
    """Static checks on tool.py. `errors` block it; `review` is shown to whoever approves it."""
    errors: List[str] = []
    review: List[str] = []
    warnings: List[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"errors": [f"does not compile: line {e.lineno}: {e.msg}"], "review": [], "warnings": [], "tools": []}
    reg = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register"), None)
    if reg is None or len(reg.args.args) != 1:
        errors.append("must define a top-level `register(mcp)` function taking exactly one argument")
    if not ast.get_docstring(tree):
        warnings.append("add a module docstring saying what the capability is for")
    tools = []
    for n in ast.walk(reg) if reg else []:
        if isinstance(n, ast.FunctionDef) and n is not reg and any(
                (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool") or (isinstance(d, ast.Attribute) and d.attr == "tool")
                for d in n.decorator_list):
            tools.append(n.name)
            doc = (ast.get_docstring(n) or "")
            if len(doc) < 40:
                errors.append(f"tool {n.name}: the docstring is the only description an agent sees: say what it does, when to use it, "
                              "every argument and the return keys (>= 40 characters)")
            elif "return" not in doc.lower():
                warnings.append(f"tool {n.name}: the docstring should name the keys it returns")
            owner = (taken_tools or {}).get(n.name)
            if owner and owner != own:
                errors.append(f"tool name {n.name!r} is already provided by capability {owner}")
    if reg is not None and not tools:
        errors.append("register() declares no tool: decorate functions with @mcp.tool()")
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in FORBIDDEN_CALLS:
                errors.append(f"line {n.lineno}: {f.id}() is not allowed in a capability")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in FORBIDDEN_ATTRS:
                errors.append(f"line {n.lineno}: {f.value.id}.{f.attr}() is not allowed (use subprocess without a shell)")
            if isinstance(f, ast.Attribute) and f.attr in ("run", "Popen", "call", "check_output", "check_call"):
                for kw in n.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        errors.append(f"line {n.lineno}: subprocess with shell=True is not allowed")
            if isinstance(f, ast.Attribute) and f.attr in ("write_text", "write_bytes", "unlink", "rmtree", "remove", "rmdir"):
                review.append(f"line {n.lineno}: writes or deletes files ({f.attr})")
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for m in [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]:
                r = m.split(".")[0]
                if r in FORBIDDEN_IMPORTS:
                    errors.append(f"line {n.lineno}: import of {r} is not allowed")
                elif r in REVIEW_IMPORTS:
                    review.append(f"imports {r}: {REVIEW_IMPORTS[r]}")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and ABS_PATH.search(n.value):
            errors.append(f"line {getattr(n, 'lineno', '?')}: machine-specific path {n.value[:40]!r}: take paths as arguments")
    return {"errors": errors, "review": sorted(set(review)), "warnings": warnings, "tools": tools}


def check_skill(skill_md: str, name: str) -> List[str]:
    """A skill must parse and carry every section that makes a tool safe to hand to an agent."""
    out = []
    m = re.match(r"^---\n(.*?)\n---\n", skill_md or "", re.S)
    if not m:
        return ["SKILL.md needs a frontmatter block with name and description"]
    if not re.search(rf"^name:\s*{re.escape(name)}\s*$", m.group(1), re.M):
        out.append(f"SKILL.md frontmatter must say name: {name}")
    if not re.search(r"^description:\s*\S", m.group(1), re.M):
        out.append("SKILL.md frontmatter needs a description (it decides when the skill is loaded)")
    heads = {h.strip().lower() for h in re.findall(r"^## +(.+)$", skill_md, re.M)}
    out += [f"SKILL.md is missing the section '## {s}'" for s in SKILL_SECTIONS if s.lower() not in heads]
    return out


def check_agent(agent: dict, name: str, tools: List[str]) -> List[str]:
    """An agent runs under a fixed, limited scope: nothing it may use is left to chance."""
    out = []
    if not (agent.get("description") or "").strip():
        out.append("agent.json needs a description")
    if agent.get("level") not in LEVELS:
        out.append(f"agent.json level must be one of {LEVELS} (never a model name)")
    bad = [t for t in agent.get("tools", []) if t.split("(")[0] not in BUILTIN_AGENT_TOOLS]
    if bad:
        out.append(f"agent.json tools are built-in tools only (its MCP tools come from the capability): unknown {bad}")
    if any(t == "Bash" for t in agent.get("tools", [])):
        out.append("agent.json: bare Bash is too wide; scope it, e.g. Bash(git diff:*)")
    if name not in agent.get("skills", []):
        out.append(f"agent.json skills must include its own skill {name}")
    if agent.get("permission_mode", "default") not in ("default", "plan", "acceptEdits"):
        out.append("agent.json permission_mode must be default, plan or acceptEdits")
    if not tools:
        out.append("the capability has no MCP tool for its agent to use")
    return out


def check_pvc(pvc: dict, tools: List[str]) -> List[str]:
    """Every capability is a predictor-validator-corrector loop: the tools that predict and the tools that rule on them must be declared, and exist.
    The corrector is the skill's Corrector section, checked with the skill."""
    pvc = pvc or {}
    out = []
    pred, val = pvc.get("predictor") or [], pvc.get("validator") or []
    if not pred:
        out.append("pvc needs predictor: the tools that produce the result")
    if not val:
        out.append("pvc needs validator: the tool that rules on the result, deterministically")
    out += [f"pvc names {t}, which the capability does not define" for t in pred + val if t not in tools]
    out += [f"pvc: {t} is both predictor and validator: nothing may judge its own output" for t in set(pred) & set(val)]
    return out


_RUNNER = r'''
import json, sys, importlib.util, traceback
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location("cap_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
out = {"ok": False, "results": [], "tools": []}
try:
    spec.loader.exec_module(mod)
    from moirai_agents.core.capstore import ToolShim
    shim = ToolShim("check")
    mod.register(shim)
    out["tools"] = sorted(shim.tools)
    for chk in json.loads(sys.argv[3]):
        t = shim.tools.get(chk.get("tool"))
        r = {"tool": chk.get("tool"), "args": chk.get("args", {}), "pass": False}
        if not t:
            r["why"] = "tool not registered"; out["results"].append(r); continue
        try:
            res = t["fn"](**chk.get("args", {}))
            raw = res if isinstance(res, str) else json.dumps(res, default=str)
            r["output"] = raw[:400]
            exp = chk.get("expect", {})
            ok, why = True, []
            if "contains" in exp and exp["contains"] not in raw: ok = False; why.append("missing text %r" % exp["contains"])
            if "json" in exp:
                try: data = json.loads(raw)
                except ValueError: data = None
                def sub(a, b):
                    if isinstance(a, dict): return isinstance(b, dict) and all(k in b and sub(v, b[k]) for k, v in a.items())
                    return a == b
                if not sub(exp["json"], data): ok = False; why.append("json does not contain %r" % (exp["json"],))
            r["pass"], r["why"] = ok, "; ".join(why)
        except Exception as e:
            r["why"] = "raised %s: %s" % (type(e).__name__, e)
            if chk.get("expect", {}).get("raises"): r["pass"], r["why"] = True, ""
        out["results"].append(r)
    out["ok"] = True
except Exception:
    out["error"] = traceback.format_exc(limit=3)
print("@@RESULT@@" + json.dumps(out))
'''


def run_checks(code: str, checks: List[dict], timeout: int = 60) -> dict:
    """Import the module and run each check in a separate scrubbed Python process with a time limit."""
    if not checks:
        return {"ok": False, "error": "a capability needs at least one check (tool, args, expect)", "results": []}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "tool.py"
        f.write_text(code)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": d, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "MOIRAI_AGENTS_PIPELINES_DIR": os.path.join(d, "pipelines"), "MOIRAI_AGENTS_LEARNING_DIR": os.path.join(d, "learning")}
        try:
            r = subprocess.run([sys.executable, "-I", "-c", _RUNNER, str(f), str(PKG.parent), json.dumps(checks)],
                               capture_output=True, text=True, timeout=timeout, env=env, cwd=d)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"checks did not finish within {timeout}s", "results": []}
    m = re.search(r"@@RESULT@@(.*)", r.stdout)
    if not m:
        return {"ok": False, "error": f"checker produced no result: {(r.stderr or r.stdout)[-300:]}", "results": []}
    out = json.loads(m.group(1))
    out["all_passed"] = bool(out.get("ok")) and bool(out["results"]) and all(x["pass"] for x in out["results"])
    return out


# ------------------------------------------------------------------------------- proposals
def _learning_dir() -> Path:
    d = Path(os.environ.get("MOIRAI_AGENTS_LEARNING_DIR", "~/.moirai-agents/learning")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proposals() -> Dict[str, dict]:
    cur: Dict[str, dict] = {}
    p = _learning_dir() / "capability_proposals.jsonl"
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        cur[e["id"]] = {**cur.get(e["id"], {}), **e}
    return cur


def _append(rec: dict) -> None:
    with open(_learning_dir() / "capability_proposals.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _taken() -> Dict[str, str]:
    return {t: n for n, (_, _, m) in all_caps().items() for t in m.get("tools", [])}


def _twin(fp: str, name: str) -> Optional[str]:
    for n, (_, d, _m) in all_caps().items():
        f = d / "tool.py"
        if n != name and f.exists():
            try:
                if fingerprint(f.read_text()) == fp:
                    return n
            except SyntaxError:
                continue
    return None


def validate_bundle(name: str, tool_code: str, skill_md: str, agent: dict, checks: List[dict], pvc: Optional[dict] = None) -> dict:
    """Every structural validator, in one verdict. Used on proposals and to audit the whole store."""
    an = analyze(tool_code, _taken(), name)
    problems = list(an["errors"])
    if not an["errors"]:
        twin = _twin(fingerprint(tool_code), name)
        if twin:
            problems.append(f"a fingerprint twin of capability {twin} already exists: extend it instead")
    problems += check_skill(skill_md, name) + check_agent(agent or {}, name, an["tools"]) + check_pvc(pvc, an["tools"])
    return {"problems": problems, "analysis": an}


def propose(name: str, tool_code: str, skill_md: str, agent: dict, checks: List[dict], description: str, why: str,
            stages: Optional[List[str]] = None, layer: str = "project", kind: str = "new", user: str = "", pvc: Optional[dict] = None) -> dict:
    ex = all_caps().get(name)
    pvc = pvc or (ex[2].get("pvc") if ex else None)
    if kind == "new" and ex:
        return _err(f"capability {name!r} already exists (version {ex[2]['version']}): use capability_update")
    if kind == "update" and not ex:
        return _err(f"capability {name!r} does not exist: use capability_new")
    if not NAME_RX.match(name):
        return _err("name must be lowercase letters, digits and '_' (3-41 chars), starting with a letter")
    if layer not in ("project", "user"):
        return _err("layer must be 'project' (committed with the workspace) or 'user' (only you)")
    if not description.strip() or not why.strip():
        return _err("say what it does (description) and why it should exist (why)")
    v = validate_bundle(name, tool_code, skill_md, agent, checks, pvc)
    if v["problems"]:
        return _err(f"{len(v['problems'])} problem(s); nothing was staged", problems=v["problems"], warnings=v["analysis"]["warnings"])
    chk = run_checks(tool_code, checks)
    if not chk.get("all_passed"):
        return _err("verification failed; nothing was staged", checks=chk)
    diff = ""
    if ex:
        old = (ex[1] / "tool.py").read_text()
        diff = "".join(difflib.unified_diff(old.splitlines(True), tool_code.splitlines(True), f"v{ex[2]['version']}", "proposed", n=2))[:6000]
    pid = uuid.uuid4().hex[:12]
    _append({"id": pid, "ts": time.time(), "status": "pending", "kind": kind, "name": name, "layer": layer, "description": description,
             "why": why, "pvc": pvc, "code": tool_code, "skill": skill_md, "agent": agent, "checks": checks, "stages": stages or (ex[2].get("stages", []) if ex else []),
             "tools": v["analysis"]["tools"], "review": v["analysis"]["review"], "warnings": v["analysis"]["warnings"], "diff": diff,
             "proposed_by": user or os.environ.get("MOIRAI_AGENTS_USER", ""), "check_results": chk["results"]})
    return {"status": "ok", "id": pid, "name": name, "kind": kind, "tools": v["analysis"]["tools"], "review": v["analysis"]["review"],
            "warnings": v["analysis"]["warnings"], "checks_passed": len(chk["results"]), "diff": diff or None,
            "next": "the user approves in the Capabilities panel (or ask_user, then capability_apply with confirmed=true); nothing runs until then"}


def _snapshot(d: Path, version: int) -> None:
    dest = d / "versions" / f"v{version}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("tool.py", "SKILL.md", "agent.json", "checks.json", "capability.json"):
        if (d / f).exists():
            shutil.copy2(d / f, dest / f)


def _write_bundle(d: Path, code: str, skill: str, agent: dict, checks: List[dict], meta: dict) -> None:
    tmp = d.with_name(d.name + ".tmp-" + uuid.uuid4().hex[:6])
    tmp.mkdir(parents=True)
    if d.exists():
        shutil.copytree(d, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp / "tool.py").write_text(code)
    (tmp / "SKILL.md").write_text(skill)
    (tmp / "agent.json").write_text(json.dumps(agent, indent=1) + "\n")
    (tmp / "checks.json").write_text(json.dumps(checks, indent=1) + "\n")
    (tmp / "capability.json").write_text(json.dumps(meta, indent=1) + "\n")
    if not (tmp / "LESSONS.md").exists():
        (tmp / "LESSONS.md").write_text(f"# Lessons: {meta['name']}\n")
    old = d.with_name(d.name + ".old-" + uuid.uuid4().hex[:6])
    if d.exists():
        os.replace(d, old)
    os.replace(tmp, d)
    shutil.rmtree(old, ignore_errors=True)


def apply(pid: str, confirmed: bool, by: str = "") -> dict:
    """Approve a staged proposal: re-verify, write the version, make it live. Requires confirmed=true."""
    if not confirmed:
        return _err("not confirmed: a person must approve a capability before it runs (confirmed=true)")
    p = _proposals().get(pid)
    if not p:
        return _err(f"unknown proposal {pid}")
    if p["status"] != "pending":
        return _err(f"proposal {pid} is already {p['status']}")
    v = validate_bundle(p["name"], p["code"], p["skill"], p["agent"], p["checks"], p.get("pvc"))
    chk = run_checks(p["code"], p["checks"])
    if v["problems"] or not chk.get("all_passed"):
        return _err("re-verification at approval time failed; not activated", problems=v["problems"], checks=chk)
    ex = all_caps().get(p["name"])
    version = ex[2]["version"] + 1 if ex else 1
    who = by or os.environ.get("MOIRAI_AGENTS_USER", "")
    hist = (ex[2].get("history", []) if ex else []) + [{"version": version, "ts": time.time(), "by": who, "proposal": pid, "why": p["why"]}]
    meta = {"name": p["name"], "description": p["description"], "version": version, "enabled": True, "stages": p["stages"], "tools": p["tools"], "pvc": p.get("pvc"),
            "review": p["review"], "history": hist}
    layer = ex[0] if ex and ex[0] != "bundled" else p["layer"]
    d = layer_dir(layer) / p["name"]
    if ex and ex[0] == "bundled":          # shadowing a bundled capability starts from its files (and its lessons)
        d.parent.mkdir(parents=True, exist_ok=True)
        if not d.exists():
            shutil.copytree(ex[1], d, ignore=shutil.ignore_patterns("__pycache__"))
    if d.exists():
        _snapshot(d, version - 1)
    _write_bundle(d, p["code"], p["skill"], p["agent"], p["checks"], meta)
    _append({"id": pid, "status": "applied", "applied_by": who, "ts": time.time(), "version": version})
    _State.notify = True
    return {"status": "ok", "name": p["name"], "version": version, "tools": p["tools"], "layer": layer, "active": True,
            "note": "live now: the MCP servers and every open chat refresh by themselves"}


def reject(pid: str, note: str = "", by: str = "") -> dict:
    p = _proposals().get(pid)
    if not p:
        return _err(f"unknown proposal {pid}")
    if p["status"] != "pending":
        return _err(f"proposal {pid} is already {p['status']}")
    _append({"id": pid, "status": "rejected", "rejected_by": by, "note": note, "ts": time.time()})
    return {"status": "ok", "id": pid, "rejected": True}


def shadow(name: str) -> Path:
    """The capability's folder in a writable layer: the bundled store is never written; a user-layer copy shadows it."""
    layer, d, _ = all_caps()[name]
    if layer != "bundled":
        return d
    dest = layer_dir("user") / name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(d, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _set_enabled(name: str, on: bool) -> dict:
    ex = all_caps().get(name)
    if not ex:
        return _err(f"unknown capability {name}")
    d = shadow(name)
    ex[2]["enabled"] = on
    (d / "capability.json").write_text(json.dumps(ex[2], indent=1) + "\n")
    _State.notify = True
    return {"status": "ok", "name": name, "enabled": on}


def disable(name: str) -> dict:
    return _set_enabled(name, False)


def enable(name: str) -> dict:
    return _set_enabled(name, True)


def rollback(name: str, version: int, confirmed: bool = False) -> dict:
    """Make an earlier version current again, recorded as a NEW version so history is never rewritten."""
    if not confirmed:
        return _err("rollback needs confirmed=true")
    ex = all_caps().get(name)
    if not ex:
        return _err(f"unknown capability {name}")
    layer, d, meta = ex
    old = d / "versions" / f"v{version}"
    if not (old / "tool.py").exists():
        return _err(f"no version {version} of {name}")
    code = (old / "tool.py").read_text()
    checks = json.loads((old / "checks.json").read_text())
    chk = run_checks(code, checks)
    if not chk.get("all_passed"):
        return _err("that version no longer passes its checks; not restored", checks=chk)
    _snapshot(d, meta["version"])
    new_v = meta["version"] + 1
    meta["version"] = new_v
    meta["history"].append({"version": new_v, "ts": time.time(), "why": f"rollback to v{version}"})
    _write_bundle(d, code, (old / "SKILL.md").read_text(), json.loads((old / "agent.json").read_text()), checks, meta)
    _State.notify = True
    return {"status": "ok", "name": name, "version": new_v, "restored_from": version}


def list_capabilities() -> dict:
    active = []
    for n, (layer, d, m) in sorted(all_caps().items()):
        lessons = d / "LESSONS.md"
        active.append({"name": n, "version": m.get("version"), "enabled": m.get("enabled", True), "layer": layer, "tools": m.get("tools", []),
                       "stages": m.get("stages", []), "pvc": m.get("pvc"), "description": m.get("description", ""), "loaded": n in _State.loaded,
                       "error": (_State.loaded.get(n) or {}).get("error"),
                       "lessons": lessons.read_text().count("\n- ") if lessons.exists() else 0})
    keys = ("id", "kind", "name", "layer", "description", "why", "tools", "review", "warnings", "proposed_by", "ts", "diff")
    pend = [{k: p.get(k) for k in keys} for p in _proposals().values() if p.get("status") == "pending"]
    return {"active": active, "pending": pend}


def get(name: str) -> dict:
    ex = all_caps().get(name)
    if not ex:
        return _err(f"unknown capability {name}")
    layer, d, meta = ex
    def rd(f):
        return (d / f).read_text() if (d / f).exists() else ""
    return {"status": "ok", "name": name, "layer": layer, "meta": meta, "code": rd("tool.py"), "skill": rd("SKILL.md"), "lessons": rd("LESSONS.md"),
            "agent": json.loads(rd("agent.json") or "{}"), "checks": json.loads(rd("checks.json") or "[]")}


def verify(name: str = "", pid: str = "") -> dict:
    """Re-run every validator on a capability or a proposal."""
    if pid:
        p = _proposals().get(pid)
        if not p:
            return _err(f"unknown proposal {pid}")
        code, skill, agent, checks, target = p["code"], p["skill"], p["agent"], p["checks"], f"proposal {pid}"
        target_name, pvc = p["name"], p.get("pvc")
    else:
        g = get(name)
        if g["status"] != "ok":
            return g
        code, skill, agent, checks, target, target_name = g["code"], g["skill"], g["agent"], g["checks"], name, name
        pvc = g["meta"].get("pvc")
    v = validate_bundle(target_name, code, skill, agent, checks, pvc)
    chk = run_checks(code, checks)
    return {"status": "ok" if not v["problems"] and chk.get("all_passed") else "fail", "target": target, "problems": v["problems"],
            "warnings": v["analysis"]["warnings"], "checks": chk}


# ------------------------------------------------------------------------------- loading into the MCP server
def _fingerprint() -> str:
    h = hashlib.sha1()
    for n, (layer, d, m) in sorted(all_caps().items()):
        try:
            mt = (d / "tool.py").stat().st_mtime_ns
        except OSError:
            mt = 0
        h.update(f"{layer}|{n}|{m.get('version')}|{m.get('enabled', True)}|{mt}".encode())
    return h.hexdigest()[:12]


def refresh(tools: Dict[str, dict], force: bool = False) -> dict:
    """Make the tool table match the store. A capability that does not load keeps its last working tools, and the
    error is shown in the Capabilities panel: a broken edit never takes the server down."""
    fp = _fingerprint()
    if fp == _State.fingerprint and not force:
        return {"changed": False}
    wanted = {n: v for n, v in all_caps().items() if v[2].get("enabled", True)}
    changed = False
    for n in list(_State.loaded):
        if n not in wanted:
            for t in _State.loaded[n].get("tools", []):
                tools.pop(t, None)
            _State.loaded.pop(n)
            changed = True
    errors = {}
    for n, (layer, d, m) in wanted.items():
        path = d / "tool.py"
        try:
            stamp = (str(path), path.stat().st_mtime_ns)
        except OSError as e:
            errors[n] = str(e)
            continue
        cur = _State.loaded.get(n)
        if cur and cur.get("stamp") == stamp:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"moirai_cap_{n}_{stamp[1]}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            shim = ToolShim(n)
            mod.register(shim)
            for t in (cur or {}).get("tools", []):
                tools.pop(t, None)
            tools.update(shim.tools)
            _State.loaded[n] = {"stamp": stamp, "tools": list(shim.tools)}
            changed = True
        except Exception as e:
            errors[n] = f"{type(e).__name__}: {e}"
            if cur:
                cur["error"] = errors[n]
            else:
                _State.loaded[n] = {"stamp": stamp, "tools": [], "error": errors[n]}
    _State.fingerprint = fp
    return {"changed": changed, "loaded": sorted(_State.loaded), "errors": errors}


def take_notify() -> bool:
    f, _State.notify = _State.notify, False
    return f
