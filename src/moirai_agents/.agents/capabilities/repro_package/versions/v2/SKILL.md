---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell; generic mode (one command under moirai-service) and batch mode (Elmerfold: a generated flux batch run.sh whose per-chunk wrapper starts the service on the compute node). Load for the package stage, 'final_provenance', 'reproduce', 'flux batch', 'submit_moirai.sh', 'repro_verify'.
---

Cross-references: [[moirai_run]] [[workload_elmerfold]] [[moirai_integrate]] [[tools_moirai]]

# Reproduction package

## Rules
- `repro_build`, then `repro_verify`, then `repro_validate`. Verify, do not assert: a package counts only after it ran from a fresh copy in a clean login shell and reproduced the counts.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh` (except the standalone `check_batch.sh`), and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Show the user the isolated run's counts at the gate, and what the run did not exercise.
- Mode: `repro_build(mode="auto")` picks `batch` when the baseline or moirai_smoke outputs record `batch_script` or `submit_command`, else `generic`. Force it with `mode="batch"|"generic"`. Generic is unchanged.
- Batch mode never starts moirai-service on the login node. 04_run generates the 2-sequence smoke input (first `SMOKE_LINES` lines of the first chunk file of `CHUNK_DIR`; baseline uses `CHUNK_DIR`), generates run.sh with the app's `utils/gen_launch_script.py` (app venv python, `--submit-script` the per-scale wrapper, `--src-dir` the patched clone, `--prestage-script`, `--overwrite`, `--cwd`), loads `MODULES` (mpifileutils/0.11.1, so dbcast is on PATH in the submitting shell), runs ONE `flux batch -t <min> --queue <queue>`, `flux watch`, then `scripts/check_batch.sh`.
- A batch job's return code does NOT reflect its chunk jobs. `check_batch.sh <batch --output file> <hosts.txt>` exits 4 when the output holds `exited with exit code <non-zero>` or a line ending ` failed` (chunk job id named), 5 when hosts.txt is missing or empty (no wrapper ran, nothing traced), 6 when the `#flux: --output` file is unreadable.
- Run `repro_batch_selftest` after any change to the batch texts: it renders the package, runs 03_build and 04_run under bash with a fake flux, module, dbcast and app python in a temp dir, and the failure logic on real failing and clean run.out text.
- repro_verify in batch mode: copy outside the session, `env -i bash -l`, smoke by default, `limit` seconds (a TERM cancels the flux batch), waits on the batch job, `reuse_venv` (moirai venv) and `app_venv` (the existing application venv) as arguments. No service is started on the login node.

## Constraints
- No username, home path, session path or job id in any script. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` is outside the session and on a file system compute nodes can see.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs.
- Do not make the check pass by loosening `inputs/expected.json`.
- No more than one batch (2 nodes) per run, walltime `SMOKE_MINUTES` (30) / `BASELINE_MINUTES` (45) from config; ask before raising.

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: 04_run wraps them with `set +u`.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A non-deterministic app gives different counts: tell the user; do not hide it.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised.
- Batch mode, application environment: the package does NOT build it. The app's `setup-env.sh` must run with the `emf_data` step removed (it is not needed for the traced app), as a login-node build that takes hours, and compute nodes cannot create files under `python3.N/site-packages`. `APP_VENV` must already exist; 02_setup_env says `NOT EXERCISED` and stops with a message when it does not. The README says the same.
- Batch mode, unreplaced `<venv>` / `<out>` placeholders of the pipeline's original command are never used: batch mode does not eval that command.
- Publishing to a shared group directory: `chmod g+rX` every parent you create.

## Corrector
Predictors `repro_build`, `repro_verify`, `repro_batch_render`, validators `repro_validate`, `repro_batch_selftest`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` |
| `pinned` | install from a release or a git commit, so a version can be named |
| `isolated_run` | the log names what the session gave and the package did not: usually a module, a path or an input |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |
| batch: 04_run exit 4 | a chunk job failed though the batch returned 0: read the batch `--output` file and `traces/<scale>/service-<host>-<job>.log` |
| batch: 04_run exit 5 | no hosts.txt: the wrapper never ran (run.sh not using `--submit-script`, or bulksubmit failed); read the batch output |
| batch: 04_run exit 6 | the `#flux: --output` file is not where run.sh says: check the generator's cwd |
| batch: `run.sh does not run the moirai wrapper` | gen_launch_script.py flags changed; re-read its --help and fix 04_run via `capability_update` |
| batch: `dbcast not on PATH` | `MODULES` in config.ini, or not a login shell |
| batch: `APP_VENV ... no bin/python` | build the application environment (see Corner cases) and set `APP_VENV` |
| selftest FAIL | the named case shows which rendered text is wrong |

## Self-heal
- `moirai-service` fails only in the isolated run (generic): the library path is set in `03_build.sh` and `04_run.sh`; compare with `moirai_env.sh`.
- Batch: service fails on the compute node: `traces/<scale>/service-<host>-<job>.log`; the wrapper searches `MOIRAI_VENV/lib/python*/site-packages/dftracer/bin/dftracer_service` unless `MOIRAI_DFTRACER_SERVICE` is set.
- `patch` fails: the app is not at the pinned state.

## What was verified and what was not
- Verified in real runs (moirai_run, this pipeline): the wrapper approach, the batch failure logic, the counts in expected.json (smoke 38/58/152, baseline 190/242/760 activities/entities/edges), the timings (about 8 min each, about 270 s dbcast).
- Verified in sandbox only (repro_batch_selftest, fake flux): the rendered scripts parse, 03_build writes env file, shim and both wrappers, 04_run builds the smoke input, calls the generator with the right flags, never starts a service, and maps the real failing and clean run.out text to exit 4 and 0.
- NOT verified: the batch package has never run on the cluster. Untested: the generator accepting an absolute run.sh path after `--`; how the first chunk file is named in the split; relative `#flux: --output` resolution; the app env build; the moirai venv build (pip installs).

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts.
