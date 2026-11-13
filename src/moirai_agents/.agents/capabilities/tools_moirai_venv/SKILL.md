---
name: tools_moirai_venv
description: Build and check the moirai venv in a session on a system without one (mammoth): dftracer wheel with ONE libdftracer_core, moirai wheel rebuilt from HEAD with gcc 12, pydftracer common.py overlay, dftracer-utils. Load when explore fails on moirai/dftracer_utils, MOIRAI_AGENTS_MOIRAI_PYTHON is missing, ModuleNotFoundError dftracer_utils, two libdftracer_core copies, rocksdb compile error with gcc 10, stale moirai wheel, dftracer-compose fails on cmake, or 'GLIBCXX_3.4.30 not found' / 'DFTracer was enabled but not available' after composing.
---
Cross-references: [[app_explore]], [[moirai_integrate]], [[system_mammoth]], [[tools-pydftracer]], [[tools-dftracer]], [[capability_builder]]

# tools_moirai_venv

## Rules
- Ask the user first (app_explore gate): the user chose "Build the venv in the session on this system" over giving another path. Never edit the shared .env without showing the edit.
- Render the steps with moirai_venv_script, run them in order, then moirai_venv_check on the venv's site-packages. Steps: module python/3.12.2 venv; `pip install --pre "dftracer[modules]" dftracer-modules` (the pre-release, >=2.2.0.post57.dev0: ONE libdftracer_core, no repair); install moirai (rebuild, never trust build/ci-wheel: compare its moirai/__init__.py with python/moirai/__init__.py); overlay pydftracer common.py from branch fix/attach-to-moirai-core; compose dftracer for provenance with the application's modules loaded (moirai-compose, or dftracer-compose --enable mpi,hdf5 --disable papi,hwloc,hip,cuda: MPI and HDF5 linked from the prebuilt variants or built here), with the venv's bin first on PATH (cmake >= 3.24); check.
- The step list ends with an import test (`python -c 'import dftracer.dftracer'`): run it, and apply the libstdc++ corner case below if it fails.
- All caches (PIP_CACHE_DIR, TMPDIR, XDG_*) go under <session>/tools/cache, nothing in home.
- Compile only on one pdebug node (srun -N1 -p pdebug -t 60), install pure python on the login node.

## Constraints
- Never modify the moirai or pydftracer source trees or commit; build with a build-dir inside the session.
- No more than one node and one hour.

## Corner cases
- gcc 10 (default /usr/tce/bin) fails on rocksdb ('ninja: build stopped'): use /usr/tce/packages/gcc/gcc-12.1.1-magic/bin (CC, CXX) (confirmed 2026-11-12).
- dftracer-compose needs cmake >= 3.24; the mammoth system cmake is 3.23.1 and fails with "CMake 3.24 or higher is required" at CMakeLists.txt:38. The venv has cmake from step 3: run compose with PATH=<venv>/bin:$PATH (the step list exports it). Use `dftracer-compose --check` first (it changes nothing), then the real run (confirmed on mammoth34, 2026-11-13).
- A composition with hwloc, PAPI or the default MPI links system libraries (libhwloc, libpapi, the intel-classic mvapich runtime) into libdftracer_core. Under the anaconda python of the python/3.12.2 module `import dftracer.dftracer` can then fail with "GLIBCXX_3.4.30 not found (required by .../anaconda3-2024.06/lib/libicuuc.so.73)"; pydftracer prints "DFTracer was enabled but not available" and decorated functions log 0 events although moirai itself runs. Fix: LD_PRELOAD=/collab/usr/gapps/python/toss_4_x86_64_ib/anaconda3-2024.06/lib/libstdc++.so.6 for the python, moirai-service start and moirai-service stop (then 3 of 3 TEST events; ring name /moirai_<x>, not /dev/shm/...), exported by the integrate stage's moirai_env.sh; or compose with fewer modules (the default here already leaves out papi and hwloc: re-test the import after any change). Unverified on the mpi,hdf5-only composition.
- dftracer-utils imports as `dftracer.utils`, not `dftracer_utils`; explore_validate's dftracer_utils check must look for that.
- PYTHONPATH of the agent (moirai-agents/src) must be unset while verifying, or it shadows the venv.
- `dftracer.finalize()` called on the module raises TypeError (missing self) and then segfaults: do not call it in the test; a decorated test function must log 3 of 3 events with no core file.

## Corrector
moirai_venv_check names the failed check: not composed -> run the compose step with the application's modules loaded and the venv's cmake first on PATH; two cores -> compose (it links libdftracer_core.so to the one library), or the dftracer is older than the pre-release; moirai init differs -> rebuild/overlay; no dftracer.utils -> pip install dftracer-utils. The checks do not run Python: when they pass but decorators log 0 events, run the import test and apply the libstdc++ preload corner case. The same failure twice stops the loop.

## Self-heal
- If a tool here is wrong, capability_update it; do not work around it in shell.

## Self-learn
- What is explored here becomes a capability: see capability_builder. New systems (other compilers, python modules) are lessons here.
