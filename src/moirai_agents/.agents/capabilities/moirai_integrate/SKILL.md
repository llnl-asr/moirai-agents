---
name: moirai_integrate
description: Wire the annotated application to moirai - environment, compile and link flags, build. Load for the integrate stage.
---

# Integrate

## Rules
- `integrate_apply` writes `moirai_env.sh`; every later stage sources it.
- Pass the build through the flags: CMake and autotools read `CFLAGS`, `CXXFLAGS`, `LDFLAGS`; a plain Makefile may need `$MOIRAI_CFLAGS` and `$MOIRAI_LDFLAGS` on its command line.
- For C/C++ give `binary` to `integrate_validate`.

## Constraints
- Build only in the annotated tree.
- The application must run under the Python that has moirai. Do not install packages into another environment without asking.
- moirai needs dftracer's event-type API; versions must match the moirai build.

## Corner cases
- App with its own virtualenv: moirai must be installed there, or use that Python as `MOIRAI_AGENTS_MOIRAI_PYTHON`. Ask.
- C++ app: the header includes `dftracer/dftracer.h`, so `$MOIRAI_CFLAGS` carries dftracer's include path too.
- Link order with static libraries: `-lmoirai` goes after the objects that use it.
- Build system that drops `LDFLAGS`: pass them in its own option.

## Corrector
Predictor `integrate_apply`, validator `integrate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `python_import` | the app must use the moirai Python: fix `MOIRAI_AGENTS_MOIRAI_PYTHON` or the command, then `integrate_apply` |
| `build` | pass `$MOIRAI_CFLAGS`, `$MOIRAI_LDFLAGS` through `build_command` and re-run `integrate_apply` |
| `links_moirai` | the link step dropped `LDFLAGS`: pass them explicitly |

## Self-heal
- Undefined `moirai_*`: not linked; add `$MOIRAI_LDFLAGS`. `moirai/moirai.h` not found: add `$MOIRAI_CFLAGS`.
- `python_import` failed: read the error; a missing dependency means the wrong Python.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A build flag this app or site needs: `lesson_add` with the log line.
- A build system the flags do not reach: `capability_update`.
