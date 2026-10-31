"""Find the Python that has moirai and dftracer-utils. This package never imports them: they need newer Pythons and
compiled wheels, and capabilities run them as subprocesses."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_PROBE = r'''
import json, sysconfig, importlib.util as u
def has(name):
    try:
        return bool(u.find_spec(name))
    except ImportError:     # the parent package is missing
        return False
print(json.dumps({"python": __import__("sys").version.split()[0], "site": sysconfig.get_paths()["purelib"],
                  "moirai": has("moirai"), "dftracer_utils": has("dftracer.utils")}))
'''


def python() -> Optional[str]:
    env = os.environ.get("MOIRAI_AGENTS_MOIRAI_PYTHON")
    if env:
        return env
    svc = shutil.which("moirai-service")
    if svc:
        return str(Path(svc).parent / "python")
    return sys.executable


def locate() -> Dict[str, Any]:
    """{ok, python, service, moirai, dftracer_utils, include, lib, dft_include}, or {ok: False, error, fix}."""
    py = python()
    fix = "pip install moirai dftracer-utils into a venv and set MOIRAI_AGENTS_MOIRAI_PYTHON=<venv>/bin/python in .env"
    try:
        r = subprocess.run([py, "-c", _PROBE], capture_output=True, text=True, timeout=60)
        info = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "python": py, "error": f"cannot run {py}: {e}", "fix": fix}
    bin_dir = Path(py).parent
    svc = bin_dir / "moirai-service"
    site = Path(info["site"])
    lib = next((str(p) for p in (site / "moirai" / "lib64", site / "moirai" / "lib") if p.exists()), "")
    dft_libs = [str(p) for p in (site / "dftracer" / "lib64", site / "dftracer" / "lib") if p.exists()]
    out = {"ok": bool(info["moirai"] and svc.exists()), "dft_libs": dft_libs, "python": py, "python_version": info["python"], "service": str(svc) if svc.exists() else "",
           "moirai": info["moirai"], "dftracer_utils": info["dftracer_utils"], "include": str(site / "moirai" / "include"), "lib": lib,
           "dft_include": str(site / "dftracer" / "include")}
    if not out["ok"]:
        out.update(error=f"{py} has no usable moirai (moirai={info['moirai']}, moirai-service={svc.exists()})", fix=fix)
    return out
