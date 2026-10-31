---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell; generic apps and flux-batch apps (Elmerfold run.sh, submit-script wrapper, flux watch). Load for the package stage, or for 'batch', 'flux batch', 'reproduce', 'final_provenance'.
---

Cross-references: [[moirai_run]] [[moirai_integrate]] [[workload_elmerfold]]

# Reproduction package

## Rules
- `repro_build`, then `repro_verify`, then `repro_validate`. Verify, do not assert: a package counts only after it ran from a fresh copy in a clean login shell and reproduced the counts.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh`, and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Mode is detected: batch when the moirai_smoke or baseline outputs record `batch_script` or `submit_command`, else generic; `mode=` overrides. Generic is unchanged.
- Batch mode: no `moirai-service` on the login node. The per-chunk wrapper (`templates/submit_moirai.sh.in`, rendered by `03_build.sh`) starts it on the worker node; `04_run.sh` makes the smoke input
  (first 2 lines of the first chunk), generates `run.sh` with the app's `gen_launch_script.py` (annotated tree as `--src-dir`, wrapper as `--submit-script`, `--cwd` the app subdir), loads `mpifileutils/0.11.1`,
  `flux batch`, `flux watch`, then judges `run.out` and `hosts.txt`. Smoke scale by default; baseline is the full split.
- A batch job's return code does not reflect its chunk jobs: fail on `exited with exit code <non-zero>`, a line ending ` failed`, an empty `run.out`, or a missing or empty `hosts.txt`.
- Batch verify: `repro_verify(reuse_venv=<app venv>, reuse_moirai_venv=<moirai venv>)`. Run `repro_batch_selftest` first (no cluster needed).
- Show the user the isolated run's counts at the gate, and say what was not exercised.

## Constraints
- No username, home path, session path or job id in any script or template. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` is outside the session and on a file system compute nodes can see.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs.
- Do not make the check pass by loosening `inputs/expected.json`.
- The batch package builds only the moirai venv. The app venv needs the app's `setup-env.sh` with the `emf_data` step removed, on a login node (hours); compute nodes cannot create files under `python3.N/site-packages`. The README says so.
- The batch run is 2 nodes, exclusive, about 8 minutes (~270 s of it dbcast); ask before more than one node-allocation or an hour.

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: `04_run.sh` wraps `module load` with `set +u`.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A non-deterministic app gives different counts: tell the user; do not hide it.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised. In batch mode the app env build is never exercised by the isolated run.
- Publishing to a shared group directory: `chmod g+rX` every parent you create.
- `dbcast` must be on PATH in the submitting shell: that is why `mpifileutils/0.11.1` is loaded before `flux batch`.
- `gen_launch_script.py` needs the submit script to exist first, and `--cwd` defaults to its parent dir, so pass it.

## Corrector
Predictors `repro_build`, `repro_verify`, validator `repro_validate` (and `repro_batch_selftest`, `repro_batch_scan`). A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` |
| `pinned` | install from a release or a git commit, so a version can be named |
| `isolated_run` | the log names what the session gave and the package did not: usually a module, a path or an input |
| `isolated_run` in batch, "exited non-zero" or "chunk job failed" | read `traces/<scale>/run.out` and `service-*.log` in the isolation dir; a wrapper exit 2 or 3 means the env file, `dftracer_service` or the ring |
| `isolated_run` in batch, "no worker wrote hosts.txt" | the wrapper never ran: `--submit-script` or the batch job itself failed; read `run.out` |
| `isolated_run` in batch, "the app venv ... does not exist" | build it per README (setup-env.sh, login node) or pass `reuse_venv` and `reuse_moirai_venv` |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |
| `repro_batch_selftest` failed | the case name says which flag, input or pattern drifted; fix `BATCH_STEPS` in the tool, never the check |

## Self-heal
- `moirai-service` fails only in the isolated run: the library path is set in `03_build.sh` and `04_run.sh` (generic) or `moirai_env.sh` (batch); compare with the session's `moirai_env.sh`.
- `patch` fails: the app is not at the pinned state.
- Batch: `no module command` means the shell is not a login shell.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts.

## Verified and not verified
- Verified by `repro_batch_selftest` (stub flux, module and app python, pure renders): the real `04_run.sh` flags to `gen_launch_script.py`, the 2-line smoke input, the baseline full split, `flux batch -t/--queue/--output`, the failure scan on the real failing lines (exit code 2, a ' failed' line) and a clean one, no literal user/home/session path, wrapper parses.
- NOT verified: this batch package has never run on the cluster. The app env build, the `01_get_source` clone over ssh, `03_build` on a real moirai venv, the real `flux batch`/`flux watch`, `#flux: --output` handling of the generated run.sh, and the final counts against `inputs/expected.json` (smoke 38/58/152, baseline 190/242/760) are untested.
