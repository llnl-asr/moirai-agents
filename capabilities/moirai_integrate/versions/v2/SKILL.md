---
name: moirai_integrate
description: Wire the annotated application to moirai - environment, compile and link flags, build; checks that moirai_env.sh puts the annotated root on PYTHONPATH and that the generated provenance helper imports from it. Load for the integrate stage, or when `import _moirai_prov` fails in a run.
---
Cross-references: [[prov_annotate]] [[workload_elmerfold]]

# Integrate

## Rules
- `integrate_apply` writes `moirai_env.sh`; every later stage sources it.
- Pass the build through the flags: CMake and autotools read `CFLAGS`, `CXXFLAGS`, `LDFLAGS`; a plain Makefile may need `$MOIRAI_CFLAGS` and `$MOIRAI_LDFLAGS` on its command line.
- For C/C++ give `binary` to `integrate_validate`.
- This stage owns the PYTHONPATH rule for the generated `_moirai_prov.py`: `moirai_env.sh` must export `PYTHONPATH=<annotated root>`. The annotate stage runs before this file exists, so it lists the rule as deferred and `integrate_validate` enforces it (`env_pythonpath`, `env_import`).

## Constraints
- Build only in the annotated tree.
- The application must run under the Python that has moirai. Do not install packages into another environment without asking.
- moirai needs dftracer's event-type API; versions must match the moirai build.

## Corner cases
- App with its own virtualenv: moirai must be installed there, or use that Python as `MOIRAI_AGENTS_MOIRAI_PYTHON`. Ask.
- C++ app: the header includes `dftracer/dftracer.h`, so `$MOIRAI_CFLAGS` carries dftracer's include path too.
- Link order with static libraries: `-lmoirai` goes after the objects that use it.
- Build system that drops `LDFLAGS`: pass them in its own option.
- `python_import` injects PYTHONPATH itself; `env_import` sources `moirai_env.sh` in a clean environment and so proves the file works. Both can pass or fail apart: if only `env_import` fails, the env file is wrong. `python -I` or `-P`/`PYTHONSAFEPATH` ignore PYTHONPATH; the app must not run that way.
- The two env checks apply to Python annotations (the annotated root has `_moirai_prov.py`); a pure C/C++ app has nothing to import and skips `env_import`, but `env_pythonpath` still requires the file.

## Corrector
Predictor `integrate_apply`, validator `integrate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `python_import` | the app must use the moirai Python: fix `MOIRAI_AGENTS_MOIRAI_PYTHON` or the command, then `integrate_apply` |
| `env_pythonpath` | `moirai_env.sh` is missing or does not export PYTHONPATH with the annotated root: re-run `integrate_apply`; if it still fails, the annotate stage has no `annotated` path, re-run `annotate_apply` first |
| `env_import` | sourcing `moirai_env.sh` alone does not make `import moirai, _moirai_prov` work: read the error (wrong Python, missing dependency, helper absent), fix `MOIRAI_AGENTS_MOIRAI_PYTHON` or the annotation, `integrate_apply`; reproduce with `integrate_env_check` |
| `build` | pass `$MOIRAI_CFLAGS`, `$MOIRAI_LDFLAGS` through `build_command` and re-run `integrate_apply` |
| `links_moirai` | the link step dropped `LDFLAGS`: pass them explicitly |

## Self-heal
- Undefined `moirai_*`: not linked; add `$MOIRAI_LDFLAGS`. `moirai/moirai.h` not found: add `$MOIRAI_CFLAGS`.
- `python_import` failed: read the error; a missing dependency means the wrong Python.
- `env_import` failed with `No module named _moirai_prov`: the annotated path in `moirai_env.sh` is stale (the annotated tree was regenerated elsewhere); re-run `integrate_apply`.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A build flag this app or site needs: `lesson_add` with the log line.
- A build system the flags do not reach: `capability_update`.
- A check that belongs to this stage but was found in an earlier one (as the PYTHONPATH rule was in annotate) is moved here with a check, and the earlier stage lists it as deferred.
