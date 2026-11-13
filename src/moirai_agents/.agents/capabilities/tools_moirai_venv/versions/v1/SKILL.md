---
name: tools_moirai_venv
description: Build and check the moirai venv in a session on a system without one (mammoth): dftracer wheel with ONE libdftracer_core, moirai wheel rebuilt from HEAD with gcc 12, pydftracer common.py overlay, dftracer-utils. Load when explore fails on moirai/dftracer_utils, MOIRAI_AGENTS_MOIRAI_PYTHON is missing, ModuleNotFoundError dftracer_utils, two libdftracer_core copies, rocksdb compile error with gcc 10, stale moirai wheel.
---
Cross-references: [[app_explore]], [[moirai_integrate]], [[system_mammoth]], [[tools-pydftracer]], [[tools-dftracer]], [[capability_builder]]

# tools_moirai_venv

## Rules
- Ask the user first (app_explore gate): the user chose "Build the venv in the session on this system" over giving another path. Never edit the shared .env without showing the edit.
- Render the steps with moirai_venv_script, run them in order, then moirai_venv_check on the venv's site-packages. Steps: module python/3.12.2 venv; PyPI `pip download --pre dftracer` wheel; repair to ONE libdftracer_core (patchelf the NEEDED names to libdftracer_core.so.4.3.0, drop dftracer.libs core copies); install moirai (rebuild, never trust build/ci-wheel: compare its moirai/__init__.py with python/moirai/__init__.py); overlay pydftracer common.py from branch fix/attach-to-moirai-core; check.
- All caches (PIP_CACHE_DIR, TMPDIR, XDG_*) go under <session>/tools/cache, nothing in home.
- Compile only on one pdebug node (srun -N1 -p pdebug -t 60), install pure python on the login node.

## Constraints
- Never modify the moirai or pydftracer source trees or commit; build with a build-dir inside the session.
- No more than one node and one hour.

## Corner cases
- gcc 10 (default /usr/tce/bin) fails on rocksdb ('ninja: build stopped'): use /usr/tce/packages/gcc/gcc-12.1.1-magic/bin (CC, CXX) (confirmed 2026-11-12).
- dftracer-utils imports as `dftracer.utils`, not `dftracer_utils`; explore_validate's dftracer_utils check must look for that.
- PYTHONPATH of the agent (moirai-agents/src) must be unset while verifying, or it shadows the venv.
- `dftracer.finalize()` called on the module raises TypeError (missing self) and then segfaults: do not call it in the test; a decorated test function must log 3 of 3 events with no core file.

## Corrector
moirai_venv_check names the failed check: two cores -> repair the wheel again; moirai init differs -> rebuild/overlay; no dftracer.utils -> pip install dftracer-utils. The same failure twice stops the loop.

## Self-heal
- If a tool here is wrong, capability_update it; do not work around it in shell.

## Self-learn
- What is explored here becomes a capability: see capability_builder. New systems (other compilers, python modules) are lessons here.
