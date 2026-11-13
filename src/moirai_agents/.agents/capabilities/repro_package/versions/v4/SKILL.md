---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell; generic apps and flux-batch apps (Elmerfold) with a per-chunk moirai wrapper. Load for the package stage, or when a package ran the service on the login node, the batch job was never waited for, or a chunk job failed unnoticed.
---

Cross-references: [[moirai_run]] [[workload_elmerfold]] [[app_smoke]] [[system-detect]]

# Reproduction package

## Rules
- `repro_build`, then `repro_verify`, then `repro_validate`. Verify, do not assert: a package counts only after it ran from a fresh copy in a clean login shell and reproduced the counts.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh`, and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Two modes. `repro_build` detects batch from the moirai_smoke or baseline stage outputs (`batch_script`, `submit_command`, `mode`), or take `mode='batch'|'generic'`. Generic runs `moirai-service` in the shell of `04_run.sh`. Batch never starts a service on the login node.
- Batch mode follows what `moirai_run` proved: the app's own generator (`utils/gen_launch_script.py`, run with the app venv python) produces `run.sh` with `--submit-script <wrapper> --src-dir <patched tree>`; the wrapper (rendered by `03_build.sh` from `scripts/submit_moirai.sh.tmpl`) starts the service in the background of each chunk job's shell. `04_run.sh` makes the smoke input (first `SMOKE_LINES` lines of the first chunk), `module load mpifileutils/0.11.1` (dbcast on PATH), `flux batch -t <min> --queue <q>`, `flux watch`, then `scripts/check_batch.py`.
- A batch job's return code does not reflect its chunk jobs: `check_batch.py` fails (4) on `exited with exit code <non-zero>` or a line ending ` failed` in the `#flux: --output` file, fails (5) on a missing or empty `hosts.txt`, fails (6) on an unreadable output.
- Two venvs in batch mode: `VENV` is the app's own (no moirai in it), `MOIRAI_VENV` holds dftracer and moirai; `moirai_env.sh` and a symlink shim put them on PYTHONPATH.
- Show the user the isolated run's counts and what was not exercised at the gate.

## Constraints
- No username, home path, session path or job id in any script. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` is outside the session and on a file system compute nodes can see.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs.
- Do not make the check pass by loosening `inputs/expected.json`.
- Batch verification submits one batch job (2 nodes exclusive, ~8 min, the baseline the same); ask before a longer limit or another node count.

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: wrap them with `set +u` in the loader.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A non-deterministic app gives different counts: tell the user; do not hide it.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised. In batch mode give `reuse_venv` (app venv) and `moirai_venv` together; the full app environment build is never exercised by `repro_verify`.
- The app env needs `setup-env.sh` with the `emf_data` step removed, a login-node build (hours), and compute nodes cannot create files under any `python3.N/site-packages`: build once, then reuse.
- The batch time limit for `repro_verify` is `limit//60 - 2` minutes, so `flux cancel` fires before the job runner's own limit.
- The smoke counts only match when the smoke input is the same 2 lines: do not change `SMOKE_LINES`.
- Publishing to a shared group directory: `chmod g+rX` every parent you create.

## Corrector
Predictors `repro_build`, `repro_verify`, validator `repro_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` |
| `pinned` | install from a release or a git commit, so a version can be named |
| `no_login_service` | batch `04_run.sh` must not start a service: it belongs to the wrapper; fix `BATCH_STEPS` in the tool, then `repro_build` |
| `isolated_run`, rc 4 from `check_batch.py` | a chunk job failed: read the batch `--output` file, the chunk logs (`moirai-wrapper:` lines) and `service-<host>-<job>.log` in `traces/<scale>` |
| `isolated_run`, rc 5 | no wrapper ran: the generator did not take `--submit-script`, or the chunk jobs never started; read `run.sh` |
| `isolated_run`, rc 6 | the `#flux: --output` file is unreadable: wrong log dir, or the batch never started |
| `isolated_run`, other | the log names what the session gave and the package did not: usually a module, a path or an input |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |

## Self-heal
- `moirai-service` fails only in the isolated run: the library path is set in `03_build.sh` (batch: `moirai_env.sh`) and `04_run.sh`; compare with the session's `moirai_env.sh`.
- `patch` fails: the app is not at the pinned state.
- Batch: `dbcast: command not found` in the chunk logs means `module load mpifileutils/0.11.1` did not happen in the submitting shell.
- Run `repro_batch_selftest` after any change: it must print `PASS`.

## What was verified and what was not
- Verified (sandbox, `repro_batch_selftest`): the wrapper template renders and parses; the batch `04_run.sh` carries every generator flag, the module load, `flux batch`, `flux watch` and the check; `check_batch.py` exits 4 on the real failing run.out text (`flux-job: task(s) exited with exit code 2`, `<date>: <job> failed`), 0 on a clean one, 5 on empty or missing hosts, 6 on an unreadable output; the command parse; no literal user, workspace, session or job path in any rendered script.
- Not verified: the package has never run. The wrapper approach and the `run.sh` flags were proven in real runs of the session; the package's generator call (absolute `--config`, `-- run.sh` written to the current directory), `03_build.sh` rendering, the config loader on the batch config and the full flow under `env -i bash -l` are untested. The full app environment build is never exercised.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts.
