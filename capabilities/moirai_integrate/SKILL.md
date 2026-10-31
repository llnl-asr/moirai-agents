---
name: moirai_integrate
description: Wire the annotated application to moirai - environment, compile and link flags, build; when the app runs under its own virtualenv, builds a symlink shim so the app's Python imports moirai without moirai being installed into the app venv. Checks that moirai_env.sh puts the shim and annotated root on PYTHONPATH and that the generated provenance helper imports under the APP's Python. Load for the integrate stage, or when `import moirai`/`import _moirai_prov` fails in a run, or a package of the app venv is shadowed.
---
Cross-references: [[prov_annotate]] [[workload_elmerfold]] [[moirai_run]]

# Integrate

## Rules
- `integrate_apply` writes `moirai_env.sh`; every later stage sources it.
- Pass the build through the flags: CMake and autotools read `CFLAGS`, `CXXFLAGS`, `LDFLAGS`; a plain Makefile may need `$MOIRAI_CFLAGS` and `$MOIRAI_LDFLAGS` on its command line.
- For C/C++ give `binary` to `integrate_validate`.
- This stage owns the PYTHONPATH rule for the generated `_moirai_prov.py`: `moirai_env.sh` must export a PYTHONPATH naming the annotated root. The annotate stage runs before this file exists, so it lists the rule as deferred and `integrate_validate` enforces it (`env_pythonpath`, `env_import`).
- Always pass `app_python` when the app runs under a Python other than the moirai Python (Elmerfold: `app_python=/p/lustre5/haridev/elmerfold/openfoldenv/bin/python`). Standing user rule: the moirai venv is SEPARATE and moirai is never installed into the app venv. A check that runs the moirai venv's own Python proves nothing about the app.
- With `app_python` in a different environment, `integrate_apply` builds `<provenance>/moirai_shim/`: symlinks only, to the top-level entries (from `importlib.metadata`: top_level.txt and RECORD, never hardcoded) of moirai and its requirement closure (for moirai today: `moirai`, `dftracer`, `dftracer.libs`). A name the app Python already provides from its own prefix is skipped and listed in `shim_skipped`, so nothing is shadowed. Rebuilding is idempotent and removes stale links. A shim inside the user's source tree is refused.
- With the shim, `moirai_env.sh` exports `PYTHONPATH=<shim>:<annotated root>`, never the moirai site-packages (it would shadow the app's typing_extensions, packaging, setuptools); it does NOT put the moirai venv bin first on PATH (a bare `python` would be the wrong interpreter): it exports `MOIRAI_SERVICE=<moirai venv>/bin/moirai-service` and appends the bin dir at the END of PATH; LD_LIBRARY_PATH is appended to, never replaced.
- Order of work: `integrate_shim_selftest` (sandbox logic), `integrate_apply(..., app_python=...)`, `integrate_validate`.

## Constraints
- Build only in the annotated tree.
- Never install packages into the app environment, and never put anything but symlinks in the shim.
- The moirai Python and the app Python must have the same minor version (compiled wheels): `python_versions` in the apply result shows both.
- moirai needs dftracer's event-type API; versions must match the moirai build.
- UNVERIFIED, left to the moirai_smoke stage: that moirai-service and the run's libraries load under the job's LD_LIBRARY_PATH. `submit_smsp.sh` puts `${ELMER_ENV}/lib` first, which may shadow libstdc++ for dftracer (symptom: `GLIBCXX_x.y.z not found` loading dftracer or `libmoirai`). This stage cannot see it: the checks here run on the login node with the env file alone. If moirai_smoke shows it, put the dftracer/moirai lib dirs before `${ELMER_ENV}/lib` in the job script, or preload the newer libstdc++; do not change the shim.

## Corner cases
- App with its own virtualenv: use `app_python`; do not install moirai there. Without `app_python` the moirai Python is assumed to be the app's (old behaviour).
- If the moirai site-packages is one of the app's own sites, no shim is made (same environment).
- The shim holds directories, not `.pth` files: a dependency installed with a `.pth` or editable hook is not carried over; `env_import` will say so.
- C++ app: the header includes `dftracer/dftracer.h`, so `$MOIRAI_CFLAGS` carries dftracer's include path too.
- Link order with static libraries: `-lmoirai` goes after the objects that use it.
- Build system that drops `LDFLAGS`: pass them in its own option.
- `python_import` injects PYTHONPATH itself with the moirai Python; `env_import` sources `moirai_env.sh` in a clean environment (under the app Python when `app_python` is set) and so proves the file works. Both can pass or fail apart. `python -I` or `-P`/`PYTHONSAFEPATH` ignore PYTHONPATH; the app must not run that way.
- The env checks apply to Python annotations (the annotated root has `_moirai_prov.py`); a pure C/C++ app skips `env_import` and the checks after it, but `env_pythonpath` and `env_path_order` still apply.
- Pending lesson L-0e661662 (moirai_integrate) is the rule above: the integrate check must run under the app's Python, in a clean env, with a shim and not the moirai site-packages. When it is approved its text and this section must agree.

## Corrector
Predictor `integrate_apply`, validator `integrate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `python_import` | the app must use the moirai Python: fix `MOIRAI_AGENTS_MOIRAI_PYTHON` or the command, then `integrate_apply` |
| `env_pythonpath` | `moirai_env.sh` is missing or does not export PYTHONPATH with the annotated root: re-run `integrate_apply`; if it still fails, the annotate stage has no `annotated` path, re-run `annotate_apply` first |
| `shim_links` | the shim is empty, has dead links or differs from the plan (moirai venv moved or upgraded): re-run `integrate_apply` with `app_python`; it rebuilds from the metadata |
| `env_path_order` | the moirai venv bin is ahead of the app's on PATH: the env file is the old kind; re-run `integrate_apply` with `app_python` (it appends the bin and exports `MOIRAI_SERVICE`) |
| `env_import` | sourcing `moirai_env.sh` alone does not make `import moirai, _moirai_prov` work under the app Python: read the error. `No module named moirai`/`dftracer`: shim absent or incomplete, re-run `integrate_apply` with `app_python`; ABI/version error: the moirai venv must be the app's Python minor version, fix `MOIRAI_AGENTS_MOIRAI_PYTHON`; `No module named _moirai_prov`: stale annotated path, re-run. Reproduce with `integrate_env_check` |
| `env_no_heavy_import` | numpy/scipy/torch loaded by the import, before the app sets `OPENBLAS_NUM_THREADS`: the shim holds more than moirai's closure or PYTHONPATH names the moirai site-packages; re-run `integrate_apply`, check `shim_links` |
| `env_no_shadow` | typing_extensions/packaging/setuptools now resolve outside the app prefix: PYTHONPATH exposes the moirai site-packages, or the shim has a name the app has; fix the env file (shim only), re-run `integrate_apply`, read `shim_skipped` |
| `build` | pass `$MOIRAI_CFLAGS`, `$MOIRAI_LDFLAGS` through `build_command` and re-run `integrate_apply` |
| `links_moirai` | the link step dropped `LDFLAGS`: pass them explicitly |

## Self-heal
- Undefined `moirai_*`: not linked; add `$MOIRAI_LDFLAGS`. `moirai/moirai.h` not found: add `$MOIRAI_CFLAGS`.
- `python_import` failed: read the error; a missing dependency means the wrong Python.
- `env_import` failed with `No module named _moirai_prov`: the annotated path in `moirai_env.sh` is stale (the annotated tree was regenerated elsewhere); re-run `integrate_apply`.
- Shim logic suspect: run `integrate_shim_selftest`; a failing row names the broken rule, repair the tool with `capability_update`.
- `GLIBCXX` not found at run time: see the libstdc++ risk in Constraints; belongs to moirai_smoke.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A build flag this app or site needs: `lesson_add` with the log line.
- A build system the flags do not reach: `capability_update`.
- A check that belongs to this stage but was found in an earlier one (as the PYTHONPATH rule was in annotate) is moved here with a check, and the earlier stage lists it as deferred.
- A check that ran under the wrong interpreter (here: the moirai Python instead of the app's) is a tool fault: fix the tool so the check runs the way the app runs, then record it.
