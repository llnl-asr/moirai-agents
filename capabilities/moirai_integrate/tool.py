"""Run pipeline stage 4: wire the annotated app to moirai: its environment, and its build against libmoirai."""
import importlib.metadata as IM
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from moirai_agents.core import jobs as J
from moirai_agents.core import moirai_env as M
from moirai_agents.core import pipeline as P

STAGE = "integrate"
HEAVY = ("numpy", "scipy", "torch")                 # the helper is imported before the app sets OPENBLAS_NUM_THREADS: none may load
APP_OWNED = ("typing_extensions", "packaging", "setuptools")    # the app venv's own; the moirai venv has other copies

_SITE_PROBE = r'''
import json, sys, sysconfig
sites = {sysconfig.get_paths()[k] for k in ("purelib", "platlib")}
sites |= {p for p in sys.path if p and p.startswith(sys.prefix) and p.endswith(("site-packages", "dist-packages"))}
print(json.dumps({"version": "%d.%d.%d" % sys.version_info[:3], "prefix": sys.prefix, "sites": sorted(sites)}))
'''

_APP_IMPORT = r'''
import json, sys
import moirai, _moirai_prov
heavy = [m for m in %r if m in sys.modules]
own = {}
for n in %r:
    try:
        own[n] = getattr(__import__(n), "__file__", None)
    except ImportError:
        own[n] = None
print(json.dumps({"heavy": heavy, "own": own, "prefix": sys.prefix, "moirai": moirai.__file__}))
''' % (HEAVY, APP_OWNED)


def _norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def _as_list(x):
    return [str(x)] if isinstance(x, (str, Path)) else [str(i) for i in x]


def _entries(dist):
    """Top-level names a distribution puts in its site-packages, from top_level.txt and RECORD, that exist on disk."""
    base = Path(str(dist.locate_file("")))
    names = set()
    for tok in (dist.read_text("top_level.txt") or "").split():
        t = tok.split("/")[0]
        for cand in (t, t + ".py"):
            if (base / cand).exists():
                names.add(cand)
        names.update(p.name for p in base.glob(t + ".*.so"))
    for f in dist.files or []:
        first = f.parts[0] if f.parts else ""
        if (first and first != ".." and first != "__pycache__" and not first.endswith((".dist-info", ".egg-info", ".data", ".pth"))
                and not first.startswith("__editable__") and (base / first).exists()):
            names.add(first)
    return base, names


def plan_shim(moirai_sites, app_sites, roots=("moirai",)):
    """Pure. Plan the symlink shim: the top-level entries of `roots` and of their requirement closure in the moirai site-packages dir(s), minus any name the app
    site-packages dir(s) already provide. Returns {links: {name: target}, skipped: [{name, dist, provided_by}], missing: [dist names not installed], dists: [...]}."""
    msites, asites = _as_list(moirai_sites), _as_list(app_sites)
    dists = {}
    for d in IM.distributions(path=msites):
        dists.setdefault(_norm(d.metadata["Name"] or ""), d)
    todo, seen, missing = [_norm(r) for r in roots], [], []
    while todo:
        n = todo.pop(0)
        if n in seen:
            continue
        d = dists.get(n)
        if d is None:
            missing.append(n)
            continue
        seen.append(n)
        for req in d.requires or []:
            body, _, marker = req.partition(";")
            if "extra" in marker:
                continue
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", body)
            if m:
                todo.append(_norm(m.group(1)))
    links, skipped = {}, []
    for n in seen:
        base, names = _entries(dists[n])
        for name in sorted(names):
            hit = next((a for a in asites if os.path.lexists(os.path.join(a, name))), None)
            if hit:
                skipped.append({"name": name, "dist": n, "provided_by": hit})
            else:
                links[name] = str(base / name)
    return {"links": links, "skipped": skipped, "missing": sorted(set(missing)), "dists": seen}


def _inside(path, roots):
    p = Path(path).resolve()
    return any(p == Path(r).resolve() or Path(r).resolve() in p.parents for r in roots if r)


def build_shim(shim, plan, forbidden=()):
    """Make `shim` hold exactly the plan's symlinks. Idempotent. Refuses a shim inside any `forbidden` tree (the user's source). Returns {created, kept, removed}."""
    if _inside(shim, forbidden):
        raise ValueError(f"{shim} is inside the user's source tree; the shim belongs in the pipeline's provenance folder")
    shim = Path(shim)
    shim.mkdir(parents=True, exist_ok=True)
    out = {"created": [], "kept": [], "removed": []}
    for p in sorted(shim.iterdir()):
        if p.name not in plan["links"]:
            if not p.is_symlink():
                raise ValueError(f"{p} is not a symlink: the shim holds only symlinks")
            p.unlink()
            out["removed"].append(p.name)
    for name, target in plan["links"].items():
        p = shim / name
        if p.is_symlink() and os.readlink(p) == target:
            out["kept"].append(name)
            continue
        if p.is_symlink():
            p.unlink()
        elif p.exists():
            raise ValueError(f"{p} exists and is not a symlink")
        os.symlink(target, p)
        out["created"].append(name)
    return out


def _probe(py):
    r = subprocess.run([py, "-c", _SITE_PROBE], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout)


def _env_rows(m, annotated, shim="", app=False):
    """The lines of moirai_env.sh. With `app` (the app runs under its own Python): the moirai bin goes at the END of PATH, MOIRAI_SERVICE names the service,
    PYTHONPATH is shim then annotated root, LD_LIBRARY_PATH is appended to."""
    q = shlex.quote
    bin_dir = str(Path(m["python"]).parent)
    libs = ([m["lib"]] if m.get("lib") else []) + list(m.get("dft_libs", []))
    if app:
        rows = [f"export MOIRAI_SERVICE={q(m.get('service') or str(Path(bin_dir) / 'moirai-service'))}",
                f"export PATH=\"$PATH\":{q(bin_dir)}",
                f"export PYTHONPATH={q(shim)}:{q(annotated)}\"${{PYTHONPATH:+:$PYTHONPATH}}\""]
        if libs:
            rows.append(f"export LD_LIBRARY_PATH=\"${{LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}}\"{q(':'.join(libs))}")
    else:
        rows = [f"export PATH={q(bin_dir)}:\"$PATH\"", f"export PYTHONPATH={q(annotated)}:\"${{PYTHONPATH:-}}\""]
        if libs:                        # dftracer_service finds its libraries only through this: the wheel's RPATH is relative to the working directory
            rows.append(f"export LD_LIBRARY_PATH={q(':'.join(libs))}:\"${{LD_LIBRARY_PATH:-}}\"")
    if m.get("lib"):
        inc = f"-I{m['include']} -I{m['dft_include']}"
        rows += [f"export MOIRAI_CFLAGS={q(inc)}", f"export MOIRAI_LDFLAGS={q('-L' + m['lib'] + ' -lmoirai -Wl,-rpath,' + m['lib'])}",
                 "export CFLAGS=\"$MOIRAI_CFLAGS ${CFLAGS:-}\" CXXFLAGS=\"$MOIRAI_CFLAGS ${CXXFLAGS:-}\" LDFLAGS=\"$MOIRAI_LDFLAGS ${LDFLAGS:-}\""]
    return rows


def _env_script(pipeline, m, annotated, shim="", app=False):
    f = P.work(P.load(pipeline)) / "moirai_env.sh"
    f.write_text("# Generated by moirai-agents: the environment of the annotated app.\n" + "\n".join(_env_rows(m, annotated, shim, app)) + "\n")
    return f


def _sourced(env, body, args, path_first=""):
    """Source `env` in a clean environment (no inherited PYTHONPATH), then run `body` with `args`. path_first: the app's bin dir, as an activated venv has it."""
    path = os.environ.get("PATH", "")
    if path_first:
        path = path_first + os.pathsep + path
    with tempfile.TemporaryDirectory() as d:
        return subprocess.run([shutil.which("bash") or "bash", "-c", '. "$1" && shift && ' + body, "bash", str(env), *args], cwd=d,
                              capture_output=True, text=True, timeout=60, env={"PATH": path, "HOME": d})


def _env_checks(env, annotated, python, app_python=""):
    """The checks of the generated environment: [(name, ok, detail, fix)]. The env file must exist and name the annotated root on PYTHONPATH; when the annotated
    tree has the generated helper, `import moirai, _moirai_prov` must resolve after sourcing it in a clean environment. With app_python that import runs under the
    APP's interpreter, and the app keeps PATH first, loads no numpy/scipy/torch, and keeps its own typing_extensions/packaging/setuptools."""
    env = Path(env)
    fix = "integrate_apply must export PYTHONPATH=<annotated root> in moirai_env.sh; re-run it"
    if not env.is_file():
        return [("env_pythonpath", False, f"{env} does not exist", "run integrate_apply: it writes moirai_env.sh")]
    text = env.read_text()
    named = "PYTHONPATH" in text and str(annotated) in text
    rows = [("env_pythonpath", named, f"{env} does not export PYTHONPATH with {annotated}", fix)]
    app = str(app_python or "")
    if app:
        appbin = str(Path(app).parent)
        r = _sourced(env, 'printf %s "${PATH%%:*}"', [], appbin)
        rows.append(("env_path_order", r.returncode == 0 and r.stdout == appbin, f"first PATH entry after sourcing is {r.stdout!r}, not {appbin}",
                     "moirai_env.sh must APPEND the moirai venv bin to PATH (and export MOIRAI_SERVICE); re-run integrate_apply with app_python so the app's Python stays first"))
    if named and (Path(annotated) / "_moirai_prov.py").exists():
        if not app:
            r = _sourced(env, '"$1" -c "import moirai, _moirai_prov"', [str(python)])
            rows.append(("env_import", r.returncode == 0, (r.stderr or r.stdout)[-300:],
                         "after `source moirai_env.sh` the moirai Python must import moirai and _moirai_prov: same interpreter as the app, PYTHONPATH naming the annotated root"))
        else:
            r = _sourced(env, '"$1" -c "$2"', [app, _APP_IMPORT], str(Path(app).parent))
            try:
                info = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                info = None
            rows.append(("env_import", r.returncode == 0 and info is not None, (r.stderr or r.stdout)[-300:],
                         f"`import moirai, _moirai_prov` failed under the app Python {app}: read the error. No module named moirai/dftracer: the shim is missing or incomplete, re-run integrate_apply "
                         "with app_python; ABI or version error: the moirai venv must be the same Python minor version as the app; No module named _moirai_prov: annotated path stale"))
            if info is not None:
                heavy = info["heavy"]
                rows.append(("env_no_heavy_import", not heavy, f"importing moirai and the helper pulled in {heavy}",
                             "the helper is imported before OPENBLAS_NUM_THREADS is set: a library loaded by the shim brought numpy/scipy/torch with it; shim only the packages in moirai's requirement closure (plan_shim) and do not put the moirai site-packages on PYTHONPATH"))
                pre = os.path.realpath(info["prefix"])
                bad = {n: f for n, f in info["own"].items() if f and not os.path.realpath(f).startswith(pre + os.sep)}
                rows.append(("env_no_shadow", not bad, f"resolve outside the app prefix {pre}: {bad}",
                             "a package the app venv provides is now served from the moirai venv: PYTHONPATH must hold only the shim (symlinks), never the moirai site-packages; plan_shim skips names the app has"))
    return rows


def register(mcp):
    @mcp.tool()
    def integrate_apply(pipeline: str, build_command: str = "", launcher: str = "", wait: int = 900, limit: int = 3600, app_python: str = "") -> dict:
        """Write <pipeline>/moirai_env.sh (PATH, PYTHONPATH, compile and link flags) and build the annotated copy with it. Use after annotate. build_command:
        reads CFLAGS/CXXFLAGS/LDFLAGS, or use $MOIRAI_CFLAGS and $MOIRAI_LDFLAGS; empty for pure Python. limit: seconds before the job is stopped. app_python: the interpreter the
        APP runs under (empty = the moirai Python is the app's Python, the old behaviour). When it is a different environment, moirai is never installed into it: this builds
        <provenance>/moirai_shim/ of symlinks to the top-level packages of moirai's requirement closure (names the app already provides are skipped and reported), and the env file
        exports PYTHONPATH=<shim>:<annotated root>, appends the moirai bin to the END of PATH, exports MOIRAI_SERVICE, and appends to LD_LIBRARY_PATH.
        Returns {status, env, job, rc, log_tail, shim, shim_links, shim_skipped, python_versions}."""
        c = P.load(pipeline)
        annotated = P.stage(c, "annotate")["outputs"].get("annotated")
        if not annotated:
            return {"status": "error", "error": "the annotate stage has no annotated tree"}
        m = M.locate()
        if not m.get("ok"):
            return {"status": "error", "error": m.get("error"), "fix": m.get("fix")}
        shim, plan, versions, app = "", None, {}, False
        if app_python:
            try:
                mi, ai = _probe(m["python"]), _probe(app_python)
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                return {"status": "error", "error": f"cannot probe {app_python} or {m['python']}: {e}", "fix": "pass the app's interpreter path (its venv bin/python)"}
            versions = {"moirai": mi["version"], "app": ai["version"]}
            if not set(mi["sites"]) & set(ai["sites"]):
                app = True
                shim = str(P.work(c) / "moirai_shim")
                plan = plan_shim(mi["sites"], ai["sites"])
                if "moirai" not in plan["dists"]:
                    return {"status": "error", "error": f"moirai is not an installed distribution in {mi['sites']}", "fix": "set MOIRAI_AGENTS_MOIRAI_PYTHON to the venv that has moirai"}
                try:
                    build_shim(shim, plan, [c.get("source", ""), P.source_of(c)])
                except ValueError as e:
                    return {"status": "error", "error": str(e), "fix": "the shim is written only under <provenance>/moirai_shim"}
        env = _env_script(pipeline, m, annotated, shim, app)
        out = {"env": str(env), "annotated": annotated, "moirai": {k: m.get(k) for k in ("python", "service", "lib")}, "build_command": build_command,
               "app_python": app_python if app else "", "python_versions": versions}
        if plan:
            out.update(shim=shim, shim_links=sorted(plan["links"]), shim_skipped=plan["skipped"], shim_missing=plan["missing"])
        P.record(pipeline, STAGE, status="running", outputs=out, note="environment written")
        if not build_command:
            return {"status": "ok", **out, "note": "nothing to build"}
        j = J.start(pipeline, STAGE, "build-annotated", f"set -e\nsource {shlex.quote(str(env))}\n{launcher} {build_command}\n", annotated, limit=limit)
        P.record(pipeline, STAGE, outputs={"job": j["id"]}, note=f"building: {build_command}")
        r = J.wait(pipeline, j["id"], wait)
        return {"status": r["status"], "env": str(env), "job": j["id"], "rc": r["rc"], "log_tail": r.get("log_tail", "")[-3000:], **{k: v for k, v in out.items() if k.startswith("shim")}}

    @mcp.tool()
    def integrate_env_check(env_file: str, annotated: str, python: str, app_python: str = "") -> dict:
        """Check a generated moirai_env.sh without a pipeline. Use to reproduce an env_pythonpath, env_import, env_path_order, env_no_heavy_import or env_no_shadow failure. env_file: path
        of moirai_env.sh. annotated: the annotated root that must be on PYTHONPATH. python: the moirai Python interpreter. app_python: the interpreter the app runs under (empty = same as
        python). env_pythonpath: the file exists and exports PYTHONPATH naming the annotated root. With app_python: env_path_order (the app's bin is still first on PATH after sourcing).
        env_import (only when the annotated root has _moirai_prov.py): after sourcing in a clean environment, `import moirai, _moirai_prov` works (under app_python when given);
        env_no_heavy_import (numpy/scipy/torch not loaded by it); env_no_shadow (typing_extensions, packaging, setuptools still come from the app prefix).
        Returns {status: pass|fail, checks: [{name, ok, detail, fix}]}."""
        rows = [{"name": n, "ok": bool(ok), "detail": d[:300], "fix": "" if ok else f[:300]} for n, ok, d, f in _env_checks(env_file, annotated, python, app_python)]
        return {"status": "pass" if all(r["ok"] for r in rows) else "fail", "checks": rows}

    @mcp.tool()
    def integrate_validate(pipeline: str, binary: str = "") -> dict:
        """Validate the integration: the moirai Python can import moirai and the generated helper, moirai_env.sh names the annotated root on PYTHONPATH and
        `import moirai, _moirai_prov` resolves from that env alone (env_pythonpath, env_import: the rule the annotate stage defers to here), under the APP's Python when integrate_apply
        got app_python (plus shim_links, env_path_order, env_no_heavy_import, env_no_shadow), the build finished with exit 0, and a binary (path relative to the annotated tree, for
        C/C++) links libmoirai. Use after integrate_apply. Returns the verdict {status, failed, unknown, passed, total}."""
        c = P.load(pipeline)
        o = P.stage(c, STAGE)["outputs"]
        v = P.Verdict(STAGE)
        if not v.check("env", bool(o.get("env")), "integrate_apply has not run", "run integrate_apply"):
            return P.finish_validation(pipeline, v)
        py = o["moirai"]["python"]
        if (Path(o["annotated"]) / "_moirai_prov.py").exists():
            r = subprocess.run([py, "-c", "import moirai, _moirai_prov"], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "PYTHONPATH": o["annotated"] + os.pathsep + os.environ.get("PYTHONPATH", "")})
            v.check("python_import", r.returncode == 0, r.stderr[-300:], "the moirai Python must match the app's Python: same interpreter, same dependencies")
        if o.get("shim"):
            sh = Path(o["shim"])
            links = sorted(p.name for p in sh.iterdir()) if sh.is_dir() else []
            dead = [n for n in links if not (sh / n).exists()]
            v.check("shim_links", bool(links) and not dead and links == sorted(o.get("shim_links", [])), f"shim {sh} holds {links}, dead {dead}, expected {o.get('shim_links')}",
                    "re-run integrate_apply with app_python: it rebuilds the shim from the moirai venv's metadata")
        for name, ok, detail, fix in _env_checks(o["env"], o["annotated"], py, o.get("app_python", "")):
            v.check(name, ok, detail, fix)
        if o.get("job"):
            j = J.get(pipeline, o["job"], 1000)
            if j["status"] == "running":
                v.inconclusive("build", "still running")
            else:
                v.check("build", j["rc"] == 0, f"rc={j['rc']}: {j.get('log_tail', '')[-300:]}", "the build must use the flags in moirai_env.sh")
        if binary:
            r = subprocess.run(["ldd", str(Path(o["annotated"]) / binary)], capture_output=True, text=True, timeout=30)
            v.check("links_moirai", "libmoirai" in r.stdout, r.stdout[-200:] or r.stderr[-200:], "add $MOIRAI_LDFLAGS to the link step")
        P.record(pipeline, STAGE, outputs={"summary": "moirai wired in" + (f", built {binary}" if binary else "")})
        return P.finish_validation(pipeline, v)

    @mcp.tool()
    def integrate_shim_selftest() -> dict:
        """Self-test of the shim logic on two fake site-packages dirs built in a temp dir (no real venvs). Use to check plan_shim, the symlink shim, and moirai_env.sh before trusting
        integrate_apply. Asserts: exactly the closure's top-level names are linked (moirai, dftracer, dftracer.libs; not pip/wheel), a name the app site has is skipped and reported,
        the shim is idempotent and removes stale links, a shim inside the source tree is refused, the env file keeps the app's PATH entry first and appends to LD_LIBRARY_PATH, a
        moirai-bin-first env fails env_path_order, and (when the interpreter runs in a clean env) the import resolves through the shim. Returns {status, checks: [{name, ok, detail}]}."""
        rows = []

        def chk(name, ok, detail=""):
            rows.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ms, ap, src = d / "msite", d / "asite", d / "source"
            for p in (ms, ap, src):
                p.mkdir()

            def dist(site, name, requires, record, top=None):
                di = site / f"{name}-1.0.dist-info"
                di.mkdir()
                (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n" + "".join(f"Requires-Dist: {r}\n" for r in requires))
                (di / "RECORD").write_text("".join(f"{r},,\n" for r in record + [f"{name}-1.0.dist-info/METADATA"]))
                if top:
                    (di / "top_level.txt").write_text(top)

            for pkg in ("moirai", "dftracer", "dftracer.libs", "packaging", "pip", "wheel"):
                (ms / pkg).mkdir()
                (ms / pkg / "__init__.py").write_text("")
            (ap / "packaging").mkdir()
            dist(ms, "moirai", ["dftracer>=1", "packaging", "pytest; extra == 'test'"], ["moirai/__init__.py"], "moirai\n")
            dist(ms, "dftracer", [], ["dftracer/__init__.py", "dftracer.libs/__init__.py", "../../bin/x"])
            dist(ms, "packaging", [], ["packaging/__init__.py"], "packaging\n")
            dist(ms, "pip", [], ["pip/__init__.py"], "pip\n")
            plan = plan_shim(ms, ap)
            chk("plan_links", sorted(plan["links"]) == ["dftracer", "dftracer.libs", "moirai"], sorted(plan["links"]))
            chk("plan_skips_app_name", [s["name"] for s in plan["skipped"]] == ["packaging"], plan["skipped"])
            chk("plan_no_missing", plan["missing"] == [], plan["missing"])
            shim = d / "prov" / "moirai_shim"
            r1 = build_shim(shim, plan, [src])
            os.symlink(str(ms / "pip"), shim / "stale")
            r2 = build_shim(shim, plan, [src])
            chk("shim_symlinks", sorted(p.name for p in shim.iterdir()) == ["dftracer", "dftracer.libs", "moirai"] and all(p.is_symlink() for p in shim.iterdir()), r1)
            chk("shim_idempotent", r2["created"] == [] and r2["removed"] == ["stale"] and len(r2["kept"]) == 3, r2)
            try:
                build_shim(src / "moirai_shim", plan, [src])
                chk("shim_refused_in_source", False, "no error")
            except ValueError as e:
                chk("shim_refused_in_source", "source tree" in str(e), e)
            appbin, mbin = d / "appbin", d / "mbin"
            for b in (appbin, mbin):
                b.mkdir()
                (b / "python").write_text("#!/bin/sh\n")
                (b / "python").chmod(0o755)
            m = {"python": str(mbin / "python"), "service": str(mbin / "moirai-service"), "lib": str(ms / "moirai" / "lib64"), "dft_libs": [str(ms / "dftracer" / "lib64")],
                 "include": "/i", "dft_include": "/j"}
            (src / "_moirai_prov.py").write_text("")
            env = d / "moirai_env.sh"
            env.write_text("\n".join(_env_rows(m, str(src), str(shim), True)) + "\n")
            r = subprocess.run([shutil.which("bash") or "bash", "-c", '. "$1"; printf "%s\\n%s\\n%s\\n%s\\n" "${PATH%%:*}" "${PATH##*:}" "$LD_LIBRARY_PATH" "$PYTHONPATH:$MOIRAI_SERVICE"',
                                "bash", str(env)], capture_output=True, text=True, env={"PATH": f"{appbin}:/usr/bin:/bin", "HOME": str(d), "LD_LIBRARY_PATH": "/keep"})
            out = r.stdout.split("\n")
            chk("env_app_first", out[0] == str(appbin), r.stdout + r.stderr)
            chk("env_moirai_bin_last", len(out) > 1 and out[1] == str(mbin), out[1:2])
            chk("env_ld_appended", len(out) > 2 and out[2].startswith("/keep:") and str(ms / "moirai" / "lib64") in out[2], out[2:3])
            chk("env_pythonpath_order", len(out) > 3 and out[3].startswith(f"{shim}:{src}:") and out[3].endswith(str(mbin / "moirai-service")), out[3:4])
            bad = d / "bad_env.sh"
            bad.write_text("\n".join(_env_rows(m, str(src), "", False)) + "\n")
            row = {n: ok for n, ok, _, _ in _env_checks(bad, str(src), str(mbin / "python"), str(appbin / "python"))}
            chk("detects_moirai_bin_first", row.get("env_path_order") is False, row)
            good = {n: ok for n, ok, _, _ in _env_checks(env, str(src), str(mbin / "python"), str(appbin / "python"))}
            chk("good_env_path_order", good.get("env_path_order") is True, good)
            probe = subprocess.run([sys.executable, "-c", "import sys"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(d)})
            if probe.returncode == 0:
                e = {"PATH": "/usr/bin:/bin", "HOME": str(d), "PYTHONPATH": f"{shim}:{src}"}
                r = subprocess.run([sys.executable, "-c", "import moirai, sys; print(moirai.__file__); print([m for m in %r if m in sys.modules])" % (HEAVY,)], capture_output=True, text=True, env=e)
                chk("import_via_shim", r.returncode == 0 and str(shim) in r.stdout and r.stdout.strip().endswith("[]"), r.stdout + r.stderr)
        return {"status": "pass" if all(r["ok"] for r in rows) else "fail", "checks": rows}
