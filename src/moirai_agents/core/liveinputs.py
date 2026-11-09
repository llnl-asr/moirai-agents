"""The inputs of an app's live runs: what a domain scientist can tune when they start one (an input file, a mode, seeds,
sizes), as dropdowns and fields in the Live Run view. Written in the annotation phase, when the agent has explored the app,
read its code and annotated it - the only time that knowledge exists - and kept on the run pipeline.

    detect(c)        candidates found deterministically: argparse arguments (with choices, defaults, types), parameters the
                     annotation reads (resolve(kwargs, 'x', env, 'X')), and files the run command names (their siblings as choices)
    check(spec)      problems of a live-inputs spec (empty when it is usable)
    write(pid, spec) validate and keep it: pipeline field ``live_inputs`` and <work>/live_inputs.json
    render(spec, values)  the exact command for chosen values (each value checked, then shell-quoted into the template)

A spec: {command: "flux batch run.sh --config {config} --mode {mode}", inputs: [{name, label, kind, options?, default?, min?,
max?, help?, required?}]}; kind is choice | integer | number | text | path | bool. Every {placeholder} in the command is an
input and every input is used in the command.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import pipeline as P

KINDS = ("choice", "integer", "number", "text", "path", "bool")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
SLOT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_FILES, MAX_OPTIONS = 400, 60
RESOLVE = re.compile(r"resolve\(\s*\w+\s*,\s*['\"](\w+)['\"]\s*,\s*\w+\s*,\s*['\"](\w+)['\"]")


# ------------------------------------------------------------------ detect
def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _argparse(tree: ast.AST, rel: str) -> List[Dict[str, Any]]:
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument" and n.args):
            continue
        flags = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not flags:
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        name = max(flags, key=len).lstrip("-").replace("-", "_")
        if not NAME.match(name) or name == "help":
            continue
        choices, default = _literal(kw["choices"]) if "choices" in kw else None, _literal(kw["default"]) if "default" in kw else None
        typ = kw["type"].id if isinstance(kw.get("type"), ast.Name) else ""
        action = _literal(kw["action"]) if "action" in kw else None
        kind = "choice" if choices else "bool" if action in ("store_true", "store_false") else "integer" if typ == "int" else "number" if typ == "float" else "text"
        out.append({"name": name, "flag": max((f for f in flags if f.startswith("-")), key=len, default=flags[0]), "kind": kind,
                    "options": [str(c) for c in choices][:MAX_OPTIONS] if choices else None, "default": default,
                    "help": _literal(kw["help"]) if "help" in kw else "", "source": f"argparse in {rel}"})
    return out


def detect(c: Dict[str, Any]) -> Dict[str, Any]:
    """Candidates for an app's live-run inputs. The agent curates them (and adds what only it knows) into a spec."""
    run = P.origin(c)
    src = Path(P.source_of(run))
    cands: List[Dict[str, Any]] = []
    seen = set()

    def add(x):
        if x["name"] not in seen:
            seen.add(x["name"])
            cands.append({k: v for k, v in x.items() if v not in (None, "")})
    # 1. argparse arguments of the app's Python files
    n = 0
    for f in sorted(src.rglob("*.py")) if src.is_dir() else []:
        if n >= MAX_FILES or any(p in f.parts for p in (".git", "venv", ".venv", "site-packages", "__pycache__")):
            continue
        n += 1
        try:
            tree = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for x in _argparse(tree, str(f.relative_to(src))):
            add(x)
    # 2. parameters the annotation reads: resolve(kwargs, 'name', env, 'ENV') in the spec's expressions
    spec = P.stage(run, "annotate")["outputs"].get("spec") or {}
    for m in RESOLVE.finditer(json.dumps(spec)):
        add({"name": m.group(1), "env": m.group(2), "kind": "text", "source": "a parameter the annotation records"})
    # 3. files the run command names: the other files beside them are the choices
    for tok in shlex.split(run.get("command", "").replace(";", " ").replace(",", " "), posix=True) if run.get("command") else []:
        p = src / tok
        if not tok.startswith("-") and "/" in tok and p.is_file():
            opts = sorted(str(q.relative_to(src)) for q in p.parent.glob("*" + p.suffix) if q.is_file())[:MAX_OPTIONS]
            add({"name": re.sub(r"\W", "_", p.parent.name or "input") + "_file", "kind": "choice" if len(opts) > 1 else "path",
                 "options": opts if len(opts) > 1 else None, "default": tok, "source": f"the file the run command names ({tok})"})
    return {"source": str(src), "command": run.get("command", ""), "candidates": cands,
            "next": "curate: keep what a scientist would tune between runs, give each a label and help, write the command template with {name} slots, then live_inputs_write"}


# ------------------------------------------------------------------ check, write, render
def _value_ok(i: Dict[str, Any], v: Any) -> Optional[str]:
    k = i["kind"]
    if v is None or v == "":
        return f"{i['name']}: a value is required" if i.get("required", True) else None
    if k == "choice" and str(v) not in [str(o) for o in i.get("options") or []]:
        return f"{i['name']}: {v!r} is not one of {i.get('options')}"
    if k in ("integer", "number"):
        try:
            x = int(v) if k == "integer" and float(v) == int(float(v)) else float(v)
        except (TypeError, ValueError):
            return f"{i['name']}: {v!r} is not a{'n integer' if k == 'integer' else ' number'}"
        if k == "integer" and float(v) != int(float(v)):
            return f"{i['name']}: {v!r} is not an integer"
        if i.get("min") is not None and x < i["min"] or i.get("max") is not None and x > i["max"]:
            return f"{i['name']}: {v} is outside [{i.get('min')}, {i.get('max')}]"
    if k == "bool" and str(v).lower() not in ("true", "false", "1", "0", "yes", "no"):
        return f"{i['name']}: {v!r} is not true or false"
    return None


def check(spec: Any) -> List[str]:
    if not isinstance(spec, dict) or not isinstance(spec.get("inputs"), list) or not isinstance(spec.get("command"), str) or not spec["command"].strip():
        return ["a live-inputs spec is {command: '<template with {name} slots>', inputs: [...]}"]
    out, names = [], []
    for j, i in enumerate(spec["inputs"]):
        if not isinstance(i, dict) or not NAME.match(str(i.get("name", ""))):
            out.append(f"input #{j + 1}: a name of letters, digits and _")
            continue
        names.append(i["name"])
        if i.get("kind") not in KINDS:
            out.append(f"{i['name']}: kind must be one of {KINDS}")
            continue
        if i["kind"] == "choice" and not (isinstance(i.get("options"), list) and i["options"]):
            out.append(f"{i['name']}: a choice needs options")
        if i.get("default") not in (None, ""):
            bad = _value_ok(i, i["default"])
            if bad:
                out.append(f"default of {bad}")
    if len(set(names)) != len(names):
        out.append(f"input names repeat: {sorted(n for n in names if names.count(n) > 1)}")
    slots = set(SLOT.findall(spec["command"]))
    if slots - set(names):
        out.append(f"the command uses {sorted(slots - set(names))}, which are not inputs")
    if set(names) - slots:
        out.append(f"inputs {sorted(set(names) - slots)} are not used in the command")
    return out


def write(pid: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    c = P.load(pid)
    if c["kind"] != "run":
        raise P.PipelineError("live inputs belong to the run pipeline that annotated the app")
    bad = check(spec)
    if bad:
        return {"status": "error", "problems": bad}
    keep = {"command": spec["command"].strip(), "inputs": [{k: v for k, v in i.items() if k in ("name", "label", "kind", "options", "default", "min", "max", "help", "required")}
                                                             for i in spec["inputs"]], "notes": str(spec.get("notes", ""))[:600]}
    P.update(pid, lambda x: x.update(live_inputs=keep))
    (P.work(c) / "live_inputs.json").write_text(json.dumps(keep, indent=1))
    return {"status": "ok", "inputs": len(keep["inputs"]), "example": render(keep, {})}


def get(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return P.origin(c).get("live_inputs")


def render(spec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """The command for these values (missing ones take their default). {status, command, values} or {status: error, problems}."""
    vals, bad = {}, []
    for i in spec["inputs"]:
        v = values.get(i["name"], i.get("default"))
        err = _value_ok(i, v)
        if err:
            bad.append(err)
            continue
        if i["kind"] == "bool":
            v = "true" if str(v).lower() in ("true", "1", "yes") else "false"
        vals[i["name"]] = "" if v is None else str(v)
    if bad:
        return {"status": "error", "problems": bad}
    cmd = SLOT.sub(lambda m: shlex.quote(vals[m.group(1)]) if vals[m.group(1)] != "" else "", spec["command"])
    return {"status": "ok", "command": re.sub(r"\s{2,}", " ", cmd).strip(), "values": vals}


def path_warnings(spec: Dict[str, Any], values: Dict[str, Any], root: str) -> List[str]:
    """Path inputs that do not exist (relative to the app): a warning, since the run may create them or run elsewhere."""
    out = []
    for i in spec["inputs"]:
        if i["kind"] == "path":
            v = str(values.get(i["name"], i.get("default")) or "")
            if v and not os.path.exists(v if os.path.isabs(v) else os.path.join(root, v)):
                out.append(f"{i['name']}: {v} does not exist here")
    return out
