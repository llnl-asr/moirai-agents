---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell; generic apps and flux-batch apps (Elmerfold) with a per-chunk moirai wrapper; then publish the verified package and its traces (baseline + in-place verification) to a shared group root with group permissions. Load for the package stage, for 'publish', 'provenance-repro', 'provenance-traces', 'chgperm', '/p/vast1/moirai', or when a package ran the service on the login node, the batch job was never waited for, or a chunk job failed unnoticed.
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

### Publishing (here: publish_root=/p/vast1/moirai, app=elmerfold, group_script=/p/vast1/moirai/chgperm.sh; always arguments, never hardcoded)
- Order: `repro_build`, `repro_batch_selftest` + `repro_publish_selftest`, `repro_publish_plan` (dry run: show it at the gate), `repro_publish_package`, `repro_verify(in_place=<published package>)`, `repro_publish_traces` for `baseline` then `verification`, `repro_publish_permissions`, `repro_publish_validate` (and `repro_validate`, which includes the same checks once a publish was recorded). The copy mode of `repro_verify` (isolation_root) stays for a check before publishing.
- Layout, as /p/vast1/dftracer-user/moirai: `provenance-repro/<app>/` is the package; `provenance-traces/<app>/<YYYYMMDD_HHMMSS of the job start>/` is one run: `raw/*.pfw.gz` (+ `.dftindex`), `README.md`, `run.json`, `run.sh`, `graph_summary.json`, and for the verification run `verification.json`.
- Timestamps are job starts: the baseline's from `flux jobs -o {t_run}` of its batch job id (found in the baseline job log, or pass `baseline_flux_job` / `baseline_timestamp`), the verification run's from its own batch job. Never the time of publishing.
- Nothing is overwritten: every target is a new directory; an existing one stops the tool.
- `repro_verify(in_place=...)` runs the published package as it is (`env -i bash -l`, only HOME/USER/TERM/SCALE plus the venv overrides you pass, which the record names); its output goes to the package's own OUTPUT_ROOT. The record `<OUTPUT_ROOT>/verification.json` holds job, scale, seconds, counts vs expected, exit codes and the package's file hashes before and after. Do not touch the package after it: the validator compares those hashes with the published files.
- `repro_publish_traces` takes every count from the trace reader (the graph tools), never typed; a fact it cannot find reads `not recorded`. `run.sh` is the exact launch script copied from the run (it can hold the run's own paths); README, run.json and graph_summary.json have the home replaced by `${HOME}`.
- `repro_publish_permissions` runs `chgperm.sh --local -q -g moirai <dir>` on the published trees only, after the user agreed. NEVER run chgperm.sh by hand or on anything else; the tool refuses a script or folder outside publish_root. Afterwards it checks group, setgid directories, g=u, no other bits.

## Constraints
- No username, home path, session path or job id in any script. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` and `in_place` are outside the session; `isolation_root` is on a file system compute nodes can see.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs. A published package is never edited either: it is verified as it is.
- Do not make the check pass by loosening `inputs/expected.json`.
- Batch verification submits one batch job (2 nodes exclusive, ~8 min, the baseline the same); ask before a longer limit or another node count. In-place verification is one smoke batch; the baseline is not rerun.
- Publishing writes only under publish_root and only new directories (plus the permission change on them).

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
- The in-place run writes to the config's OUTPUT_ROOT (`${HOME}/...` of whoever runs it): a second in-place run removes the earlier `verification.json` and traces under that root; publish the earlier run's traces first.
- chgperm adds `.chgperm_logs` inside the folders: hashes and checks skip it.
- `repro_verify(in_place)` may return before the batch ends (`wait`): call it again with the same in_place to complete the record; `repro_publish_traces(kind="verification")` needs it.
- A shared group directory: the publish tools create the parents; chgperm sets the final bits.

## Corrector
Predictors `repro_build`, `repro_verify`, `repro_publish_plan`, `repro_publish_package`, `repro_publish_traces`, `repro_publish_permissions`; validators `repro_validate`, `repro_batch_selftest`, `repro_publish_selftest`, `repro_publish_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` (a published copy that fails: tell the user; republishing needs a new target) |
| `pinned` | install from a release or a git commit, so a version can be named |
| `no_login_service` | batch `04_run.sh` must not start a service: it belongs to the wrapper; fix `BATCH_STEPS` in the tool, then `repro_build` |
| `isolated_run`, rc 4 from `check_batch.py` | a chunk job failed: read the batch `--output` file, the chunk logs (`moirai-wrapper:` lines) and `service-<host>-<job>.log` in `traces/<scale>` |
| `isolated_run`, rc 5 | no wrapper ran: the generator did not take `--submit-script`, or the chunk jobs never started; read `run.sh` |
| `isolated_run`, rc 6 | the `#flux: --output` file is unreadable: wrong log dir, or the batch never started |
| `isolated_run`, other | the log names what the session gave and the package did not: usually a module, a path or an input |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |
| plan: targets exist | publish under a new app/timestamp, or ask the user; never delete a published folder yourself |
| plan: `needs_input` | the baseline job start is unknown: pass `baseline_flux_job` (flux id) or `baseline_timestamp` |
| `published_package`, `run_folders_complete`, `published_runs` | the publish step named in the fix was skipped or failed halfway |
| `package_equals_source` | the published copy differs from final_provenance: rebuild and publish to a new target with the user's approval |
| `verification_record` | package hashes differ from the record (the package changed after the verified run) or the run failed: `repro_verify(in_place)` again |
| `readme_counts` | regenerate the run folder (new target) from the graph tools |
| `permissions` | the exceptions name each path; rerun `repro_publish_permissions`; if it persists, the script or the group is wrong (ask) |

## Self-heal
- `moirai-service` fails only in the isolated run: the library path is set in `03_build.sh` (batch: `moirai_env.sh`) and `04_run.sh`; compare with the session's `moirai_env.sh`.
- `patch` fails: the app is not at the pinned state.
- Batch: `dbcast: command not found` in the chunk logs means `module load mpifileutils/0.11.1` did not happen in the submitting shell.
- Run `repro_batch_selftest` and `repro_publish_selftest` after any change: they must print `PASS`.
- No baseline timestamp: the baseline job log did not name the flux id; ask the user for it rather than guess.
- A publish step died halfway: a `.tmp-<ts>` folder may remain under provenance-traces/<app>; report it, do not delete it unasked.

## What was verified and what was not
- Verified (sandbox, `repro_batch_selftest`): the wrapper template renders and parses; the batch `04_run.sh` carries every generator flag, the module load, `flux batch`, `flux watch` and the check; `check_batch.py` exits 4 on the real failing run.out text (`flux-job: task(s) exited with exit code 2`, `<date>: <job> failed`), 0 on a clean one, 5 on empty or missing hosts, 6 on an unreadable output; the command parse; no literal user, workspace, session or job path in any rendered script.
- Verified (sandbox, `repro_publish_selftest`: temp publish_root, fake package and traces, fake chgperm.sh, the caller's own group as the fake group): the plan writes nothing and refuses; package copy and hash compare; traces copy and README/run.json/graph_summary.json from fake counts; refusals (existing target, outside publish_root, bad names, verification without record); permission application and verification (other bits, group, setgid); the validator catches a changed package and wrong README counts.
- Not verified: the package has never run on its own from a published folder. The wrapper approach and the `run.sh` flags were proven in real runs of the session; the package's generator call, `03_build.sh` rendering and the full flow under `env -i bash -l` were exercised only by the earlier isolated runs of the session copy. Untested for publishing: `repro_verify(in_place)` end to end and its record; the real chgperm.sh (group `moirai`, setgid/g=u on the real file system, how long `--local` takes for 200+ files); the flux id discovery from the baseline job log and `flux jobs -o {t_run}`; the trace reader on the real published raw folders; the README app commit, dftracer and moirai values come from the package pins (config.ini), not from the baseline run itself; `Env:` lists only MOIRAI_/DFTRACER_ exports of the wrapper. The full app environment build is never exercised.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated or in-place run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts (chgperm behaviour, flux fields).
