---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, prove it in a clean shell, then publish the verified package and its traces (baseline + in-place verification) to a shared group root with group permissions; generic mode and batch mode (Elmerfold flux batch). Load for the package stage, 'final_provenance', 'reproduce', 'flux batch', 'repro_verify', 'publish', 'provenance-repro', 'provenance-traces', 'chgperm', '/p/vast1/moirai'.
---

Cross-references: [[moirai_run]] [[workload_elmerfold]] [[moirai_integrate]] [[tools_moirai]]

# Reproduction package

## Rules
- Order: `repro_build`, `repro_batch_selftest`/`repro_publish_selftest`, `repro_publish_plan` (dry run, show it at the gate), `repro_publish_package`, `repro_verify(in_place=<published package>)`, `repro_publish_traces` for `baseline` then `verification`, `repro_publish_permissions`, `repro_publish_validate` (and `repro_validate`, which includes it once published). The copy mode of `repro_verify` (isolation_root) stays for a quick check before publishing.
- Verify, do not assert: a package counts only after it ran from where it is published, in a clean login shell, and reproduced the counts.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh` (except the standalone `check_batch.sh`), and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Show the user the verification run's counts at the gate, and what the run did not exercise.
- Mode: `repro_build(mode="auto")` picks `batch` when the baseline or moirai_smoke outputs record `batch_script` or `submit_command`, else `generic`. Force it with `mode="batch"|"generic"`. Generic is unchanged.
- Batch mode never starts moirai-service on the login node. 04_run generates the 2-sequence smoke input (first `SMOKE_LINES` lines of the first chunk file of `CHUNK_DIR`; baseline uses `CHUNK_DIR`), generates run.sh with the app's `utils/gen_launch_script.py`, loads `MODULES` (mpifileutils/0.11.1, so dbcast is on PATH), runs ONE `flux batch -t <min> --queue <queue>`, `flux watch`, then `scripts/check_batch.sh`.
- A batch job's return code does NOT reflect its chunk jobs. `check_batch.sh <batch --output file> <hosts.txt>` exits 4 on a failed chunk job, 5 when hosts.txt is missing or empty, 6 when the `#flux: --output` file is unreadable.
- Run `repro_batch_selftest` after any change to the batch texts and `repro_publish_selftest` after any change to the publishing code.
- repro_verify in batch mode: smoke by default, `limit` seconds (a TERM cancels the flux batch), waits on the batch job, `reuse_venv` (moirai venv) and `app_venv` as arguments. No service is started on the login node.

### Publishing (publish_root = /p/vast1/moirai, app = elmerfold, group_script = /p/vast1/moirai/chgperm.sh here; always pass them as arguments)
- Layout, as /p/vast1/dftracer-user/moirai: `provenance-repro/<app>/` is the package; `provenance-traces/<app>/<YYYYMMDD_HHMMSS of the job start>/` is one run: `raw/*.pfw.gz` (+ `.dftindex`), `README.md`, `run.json`, `run.sh`, `graph_summary.json`, and for the verification run `verification.json`.
- Timestamps are job starts: the baseline's from `flux jobs -o {t_run}` of its batch job id (found in the baseline job log, or pass `baseline_flux_job`/`baseline_timestamp`), the verification run's from its own batch job. Never a clock time of publishing.
- Nothing is overwritten: every target is a new directory; an existing one stops the tool.
- `repro_verify(in_place=...)` runs the published package as it is (`env -i bash -l`, only HOME/USER/TERM/SCALE), outputs go to the package's own OUTPUT_ROOT from config.ini; the record `<OUTPUT_ROOT>/verification.json` holds job, scale, seconds, counts vs expected, exit codes and the package's file hashes before and after. Do not touch the package after it: the validator compares those hashes with the published files.
- `repro_publish_traces` takes every count from the trace reader (the graph tools), never typed; a fact it cannot find reads `not recorded`. run.sh is the exact launch script copied from the run (it can hold the run's own paths; README/run.json/graph_summary.json have the home replaced by `${HOME}`).
- `repro_publish_permissions` runs `chgperm.sh --local -q -g moirai <dir>` on the published trees only, after the user agreed. NEVER run chgperm.sh by hand or on anything else; the tool refuses a script or folder outside publish_root. Afterwards it checks group, setgid dirs, g=u, no other bits.

## Constraints
- No username, home path, session path or job id in any package script. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` is outside the session and on a file system compute nodes can see; `in_place` is outside the session too.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs. A published package is never edited either: it is verified as it is.
- Do not make the check pass by loosening `inputs/expected.json`.
- No more than one batch (2 nodes) per run, walltime `SMOKE_MINUTES` (30) / `BASELINE_MINUTES` (45) from config; ask before raising. In-place verification is one smoke batch; the baseline is not rerun.
- Publishing writes only under publish_root and only new directories (plus the permission change on them).

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: 04_run wraps them with `set +u`.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A non-deterministic app gives different counts: tell the user; do not hide it.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised.
- Batch mode, application environment: the package does NOT build it. `APP_VENV` must already exist; 02_setup_env says `NOT EXERCISED` and stops when it does not.
- Batch mode, unreplaced `<venv>` / `<out>` placeholders of the pipeline's original command are never used.
- The in-place run writes to the config's OUTPUT_ROOT (`${HOME}/moirai_repro/...` of whoever runs it): a second in-place run removes the earlier `verification.json` and the traces under that root; publish the earlier run's traces before running again.
- chgperm adds `.chgperm_logs` inside the folders: hashes and checks skip it.
- `repro_verify(in_place)` may return before the batch ends (`wait`): call it again with the same in_place to complete the record; `repro_publish_traces(kind="verification")` needs it.
- Publishing to a shared group directory: parents are created by the tools; chgperm sets the final bits.

## Corrector
Predictors `repro_build`, `repro_verify`, `repro_batch_render`, `repro_publish_plan`, `repro_publish_package`, `repro_publish_traces`, `repro_publish_permissions`; validators `repro_validate`, `repro_batch_selftest`, `repro_publish_selftest`, `repro_publish_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` (a published copy that fails: tell the user; publishing again needs a new target) |
| `pinned` | install from a release or a git commit, so a version can be named |
| `isolated_run` | the log names what the session gave and the package did not: usually a module, a path or an input |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |
| batch: 04_run exit 4 / 5 / 6 | 4 a chunk job failed though the batch returned 0 (read the batch `--output` and `service-*.log`); 5 no hosts.txt (wrapper never ran); 6 the `#flux: --output` file is not where run.sh says |
| batch: `run.sh does not run the moirai wrapper` | gen_launch_script.py flags changed; re-read its --help and fix 04_run via `capability_update` |
| batch: `dbcast not on PATH` | `MODULES` in config.ini, or not a login shell |
| batch: `APP_VENV ... no bin/python` | build the application environment and set `APP_VENV` |
| selftest FAIL | the named case shows which text or rule is wrong |
| plan: targets exist | publish under a new timestamp/app, or ask the user; never delete a published folder yourself |
| plan: `needs_input` | the baseline job start is unknown: pass `baseline_flux_job` (flux id) or `baseline_timestamp` |
| `published_package`, `run_folders_complete` | the step named in the fix was skipped or failed halfway |
| `package_equals_source` | the published copy differs from final_provenance: rebuild/publish to a new target with the user's approval |
| `verification_record` | package hashes differ from the record (the package changed after the verified run) or the run failed: repro_verify(in_place) again |
| `readme_counts` | regenerate the run folder (new target) from the graph tools |
| `permissions` | the exceptions list names each path; rerun repro_publish_permissions; if it persists, the script or the group is wrong (ask) |

## Self-heal
- `moirai-service` fails only in the isolated run (generic): the library path is set in `03_build.sh` and `04_run.sh`; compare with `moirai_env.sh`.
- Batch: service fails on the compute node: `traces/<scale>/service-<host>-<job>.log`; the wrapper searches `MOIRAI_VENV/lib/python*/site-packages/dftracer/bin/dftracer_service` unless `MOIRAI_DFTRACER_SERVICE` is set.
- `patch` fails: the app is not at the pinned state.
- No timestamp for the baseline: the baseline job log did not name the flux id; ask the user for it (`flux jobs` history) rather than guess.
- A publish step died halfway: a `.tmp-<ts>` folder under provenance-traces/<app> may remain; report it to the user, do not delete it unasked.

## What was verified and what was not
- Verified in real runs (moirai_run, this pipeline): the wrapper approach, the batch failure logic, the counts in expected.json (smoke 38/58/152, baseline 190/242/760 activities/entities/edges), the timings (about 8 min each, about 270 s dbcast).
- Verified in sandbox only (`repro_batch_selftest`, fake flux): the rendered scripts parse, 03_build, 04_run flags and failure mapping.
- Verified in sandbox only (`repro_publish_selftest`, temp publish_root, fake chgperm.sh, the caller's own group as the fake group): plan writes nothing and refuses; package copy and hash compare; traces copy and README/run.json/graph_summary.json from fake counts; refusals (existing target, outside publish_root, bad names, verification without record); permission application and verification (other bits, group, setgid); validator catches a changed package and wrong README counts.
- NOT verified: anything on the cluster (the batch package has never run there); `repro_verify(in_place)` end to end and its verification record; the real chgperm.sh (group `moirai`, setgid/g=u on the real file system, whether its --local run finishes quickly for 200+ trace files); the flux id discovery from the baseline job log and `flux jobs -o {t_run}`; the trace reader on the real published raw folders; reading the app commit/versions from config.ini (they are the pins, not necessarily what the baseline ran); `Env:` only lists MOIRAI_/DFTRACER_ exports from the wrapper.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated or in-place run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts (chgperm behaviour, flux fields).
