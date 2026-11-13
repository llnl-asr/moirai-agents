"""tools_moirai_venv: render the build steps of a moirai venv and check an installed one."""
import os
import tempfile
from pathlib import Path


def _script(tools_dir, python_module, moirai_src, pydftracer_src, gcc_bin, partition):
    t = tools_dir.rstrip("/")
    v = f"{t}/moirai-venv"
    return f"""#!/usr/bin/env bash
set -euo pipefail
unset PYTHONPATH VIRTUAL_ENV
export PIP_CACHE_DIR={t}/cache/pip TMPDIR={t}/cache/tmp XDG_CACHE_HOME={t}/cache/xdg XDG_DATA_HOME={t}/cache/data
mkdir -p {t}/cache/pip {t}/cache/tmp {t}/cache/xdg {t}/cache/data {t}/wheels
# 1. venv from the system python module
module load {python_module}
python3 -m venv {v}
{v}/bin/pip install -q --upgrade pip wheel
# 2. dftracer wheel from PyPI, repaired to ONE libdftracer_core
{v}/bin/pip download --pre --no-deps -d {t}/wheels/probe dftracer
{v}/bin/python -m wheel unpack {t}/wheels/probe/dftracer-*.whl -d {t}/wheels/repair/u
# patchelf --replace-needed libdftracer_core-<hash>.so.4.3.0 libdftracer_core.so.4.3.0 on every dftracer .so/bin, then rm dftracer.libs/libdftracer_core*
{v}/bin/python -m wheel pack {t}/wheels/repair/u/* -d {t}/wheels/repair/out
{v}/bin/pip install --pre {t}/wheels/repair/out/dftracer-*.whl
# 3. moirai: rebuild from HEAD on ONE compute node with gcc 12 (gcc 10 fails on rocksdb)
{v}/bin/pip install -q "scikit-build-core>=0.10" "pybind11>=2.12" "cmake>=3.24" ninja
srun -N1 -p {partition} -t 60 env CC={gcc_bin}/gcc CXX={gcc_bin}/g++ PATH={gcc_bin}:{v}/bin:$PATH \\
  bash -c 'cd {moirai_src} && {v}/bin/pip wheel --no-deps --no-build-isolation -w {t}/wheels/moirai -C build-dir={t}/wheels/cmake-build .'
{v}/bin/pip install --force-reinstall --no-deps {t}/wheels/moirai/moirai-*.whl
# 4. pydftracer common.py overlay (branch fix/attach-to-moirai-core) and one core symlink
SP=$({v}/bin/python -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')
cp {pydftracer_src}/python/dftracer/python/common.py $SP/dftracer/python/common.py
cp {moirai_src}/python/moirai/__init__.py $SP/moirai/__init__.py
(cd $SP/dftracer/lib64 && ln -sf libdftracer_core.so.4.3.0 libdftracer_core.so)
echo "site-packages: $SP  (now run moirai_venv_check)"
"""


def _check(sp, moirai_init=""):
    sp = Path(sp)
    res = []

    def add(name, ok, detail, fix=""):
        res.append({"check": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix})

    cores = sorted(p.name for p in sp.rglob("libdftracer_core*") if p.is_file() and not p.is_symlink() and "_dbg" not in p.name) if sp.is_dir() else []
    add("one_core", len(cores) == 1, f"{len(cores)} real libdftracer_core file(s): {cores}", "repair the dftracer wheel: patchelf the NEEDED names, delete dftracer.libs/libdftracer_core*")
    add("dftracer_utils", (sp / "dftracer" / "utils").is_dir(), "dftracer/utils present" if (sp / "dftracer" / "utils").is_dir() else "missing", "pip install --pre dftracer-utils (imports as dftracer.utils)")
    mi = sp / "moirai" / "__init__.py"
    add("moirai_installed", mi.is_file(), str(mi), "install the rebuilt moirai wheel")
    if moirai_init and mi.is_file():
        same = mi.read_text() == Path(moirai_init).read_text()
        add("moirai_current", same, "equals repo __init__.py" if same else "differs from repo __init__.py", "rebuild the wheel (build/ci-wheel is stale) or overlay python/moirai/*.py")
    c = sp / "dftracer" / "python" / "common.py"
    attach = c.is_file() and "DFTRACER_ATTACH" in c.read_text()
    add("pydftracer_attach", attach, "common.py honours DFTRACER_ATTACH" if attach else "common.py has no DFTRACER_ATTACH", "overlay common.py from branch fix/attach-to-moirai-core")
    return res


def register(mcp):
    @mcp.tool()
    def moirai_venv_script(tools_dir: str, moirai_src: str, pydftracer_src: str, python_module: str = "python/3.12.2",
                           gcc_bin: str = "/usr/tce/packages/gcc/gcc-12.1.1-magic/bin", partition: str = "pdebug") -> dict:
        """Render (never run) the shell steps that build <tools_dir>/moirai-venv: python module venv, PyPI dftracer wheel repaired to one core, moirai wheel rebuilt with gcc 12 on one compute node, pydftracer common.py overlay. Use when a system has no moirai venv. Args: tools_dir (session tools dir), moirai_src and pydftracer_src (checkouts, read only), python_module, gcc_bin, partition. Returns {status, script}."""
        for n, p in (("tools_dir", tools_dir), ("moirai_src", moirai_src), ("pydftracer_src", pydftracer_src)):
            if not p.startswith("/"):
                return {"status": "error", "problem": f"{n} must be absolute"}
        return {"status": "ok", "script": _script(tools_dir, python_module, moirai_src, pydftracer_src, gcc_bin, partition)}

    @mcp.tool()
    def moirai_venv_check(site_packages: str, moirai_init: str = "") -> dict:
        """Validate an installed moirai venv from its site-packages dir: one real libdftracer_core, dftracer/utils present, moirai installed (and equal to the repo's python/moirai/__init__.py when moirai_init is given), pydftracer common.py honours DFTRACER_ATTACH. Returns {status: pass|fail, checks: [{check, ok, detail, fix}]}."""
        r = _check(site_packages, moirai_init)
        return {"status": "pass" if all(x["ok"] for x in r) else "fail", "checks": r}

    @mcp.tool()
    def moirai_venv_selftest() -> dict:
        """Self-test moirai_venv_check on fixtures it creates: a good tree passes, two core copies and a missing dftracer/utils fail. Returns {status: pass|fail, detail}."""
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "sp"
            (sp / "dftracer" / "lib64").mkdir(parents=True)
            (sp / "dftracer" / "utils").mkdir()
            (sp / "dftracer" / "python").mkdir()
            (sp / "moirai").mkdir()
            (sp / "moirai" / "__init__.py").write_text("x=1\n")
            (sp / "dftracer" / "python" / "common.py").write_text("os.getenv('DFTRACER_ATTACH')\n")
            (sp / "dftracer" / "lib64" / "libdftracer_core.so.4.3.0").write_text("core")
            good = moirai_venv_check(str(sp))["status"]
            (sp / "dftracer.libs").mkdir()
            (sp / "dftracer.libs" / "libdftracer_core-abc.so.4.3.0").write_text("core2")
            two = moirai_venv_check(str(sp))["status"]
            (sp / "dftracer.libs" / "libdftracer_core-abc.so.4.3.0").unlink()
            (sp / "dftracer" / "utils").rmdir()
            nou = moirai_venv_check(str(sp))["status"]
        ok = (good, two, nou) == ("pass", "fail", "fail")
        return {"status": "pass" if ok else "fail", "detail": f"good={good} two_cores={two} no_utils={nou}"}
