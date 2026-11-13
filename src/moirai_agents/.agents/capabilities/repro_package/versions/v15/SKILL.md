---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell; generic apps and flux-batch apps (Elmerfold) with a per-chunk moirai wrapper; then publish the verified package and its traces (baseline + in-place verification) to a shared group root with group permissions. The standalone prov_check.py verifies activities, entity types and dangling ids, and counts entities/edges only informationally when the run captures run-dependent values. Verification and its OUTPUT_ROOT never live on home (lustre or workspace only). Load for the package stage, for 'publish', 'provenance-repro', 'provenance-traces', 'chgperm', '/p/vast1/moirai', 'prov_check', 'entities: N, the original run had M', 'Disk quota exceeded' in verification.json, or when a package ran the service on the login node, the batch job was never waited for, or a chunk job failed unnoticed.
---

Cross-references: [[moirai_run]] [[workload_elmerfold]] [[workload_biotox_repro]] [[app_smoke]] [[system-detect]]

# Reproduction package

This is the home of the generic package and publish procedure. Workload capabilities (for example [[workload_biotox_repro]]) keep only what is specific to their workload and point here for the rest.

## Rules
- Verification and its OUTPUT_ROOT never live on home; lustre or workspace only. Home (`/g/g92`, 24 GB quota) fills up: `repro_verify(in_place=<elmerfold-v10>)` failed 4 times with `OSError: [Errno 122] Disk quota exceeded` writing `<OUTPUT_ROOT>/verification.json`, because `repro_build`'s old default `output_root` was `${HOME}/moirai_repro/${APP}`, and a failed pip build had left a 41 MB half-built `moirai-venv` there (user: "verification should never happen on home. It should happen on lustre or workspace"). Run in-place verification with an `output_root` on lustre (`/p/lustre5/$USER/moirai_repro/<app>`) or a workspace folder outside the published package; `repro_build`'s default `output_root` is `/p/lustre5/${USER}/moirai_repro/${APP}` when the build machine has `/p/lustre5`, else a workspace path, with `${USER}` kept so the package stays nobody's.
- `repro_build`, then `repro_verify`, then `repro_validate`. Verify, do not assert: a package counts only after it ran in a clean login shell and reproduced the activities and entity types.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh`, and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Two modes. `repro_build` detects batch from the moirai_smoke or baseline stage outputs (`batch_script`, `submit_command`, `mode`), or take `mode='batch'|'generic'`. Generic runs `moirai-service` in the shell of `04_run.sh`. Batch never starts a service on the login node.
- Batch mode follows what `moirai_run` proved: the app's own generator (`utils/gen_launch_script.py`, run with the app venv python) produces `run.sh` with `--submit-script <wrapper> --src-dir <patched tree>`; the wrapper (rendered by `03_build.sh` from `scripts/submit_moirai.sh.tmpl`) starts the service in the background of each chunk job's shell. `04_run.sh` makes the smoke input (first `SMOKE_LINES` lines of the first chunk), `module load mpifileutils/0.11.1` (dbcast on PATH), `flux batch -t <min> --queue <q>`, `flux watch`, then `scripts/check_batch.py`.
- A batch job's return code does not reflect its chunk jobs: `check_batch.py` fails (4) on `exited with exit code <non-zero>` or a line ending ` failed` in the `#flux: --output` file, fails (5) on a missing or empty `hosts.txt`, fails (6) on an unreadable output.
- Two venvs in batch mode: `VENV` is the app's own (no moirai in it), `MOIRAI_VENV` holds dftracer and moirai; `moirai_env.sh` and a symlink shim put them on PYTHONPATH.
- What `prov_check.py` requires: activities exactly equal to `inputs/expected.json`; every expected entity type present (extra types are allowed); no dangling id; every type described; every scientific artifact type produced. `expected.json` (written by `repro_build` from the same trace reader as the counts) holds the three counts and the sorted list of entity `types` of the reference traces.
- A run that captures run-dependent values has non-deterministic entity and edge counts: verify activities, types and dangling, not entity counts. When any expected type name ends with `.input` or `.attribute` (the annotation captured run inputs and attributes: command lines, self.* fields, pointers, devices), `prov_check.py` reports entity and edge counts in `counts` and `expected` and in a `note`, and never fails on them. An `expected.json` without `types` (an older build) keeps the exact comparison of all three counts.
- Show the user the isolated run's counts and what was not exercised at the gate.

### Publishing (here: publish_root=/p/vast1/moirai, app=elmerfold, group_script=/p/vast1/moirai/chgperm.sh; always arguments, never hardcoded)
- Order: `repro_build`, `repro_batch_selftest` + `repro_publish_selftest` + `repro_check_selftest`, `repro_publish_plan` (dry run: show it at the gate), `repro_publish_package`, `repro_verify(in_place=<published package>)`, `repro_publish_traces` for `baseline` then `verification`, `repro_publish_permissions`, `repro_publish_validate` (and `repro_validate`, which includes the same checks once a publish was recorded). The copy mode of `repro_verify` (isolation_root) is an optional check before publishing; skip it when the package is verified in place (BioTox: the user asked for one verification only).
- Layout, as /p/vast1/dftracer-user/moirai: `provenance-repro/<app>/` is the package; `provenance-traces/<app>/<YYYYMMDD_HHMMSS of the job start>/` is one run: `raw/*.pfw.gz`, `raw/.dftindex` (the TraceViewer index, built in place by the publish), `README.md`, `run.json`, `run.sh`, `graph_summary.json`, and for the verification run `verification.json`.
- Timestamps are job starts: the baseline's from `flux jobs -o {t_run}` of its batch job id (found in the baseline job log, or pass `baseline_flux_job` / `baseline_timestamp`), the verification run's from its own batch job. Never the time of publishing.
- Nothing is overwritten: every target is a new directory; an existing one stops the tool.
- `repro_verify(in_place=...)` runs the published package as it is (`env -i bash -l`, only HOME/USER/TERM/SCALE plus the venv overrides you pass, which the record names); its output goes to OUTPUT_ROOT, which is never under home (see the first rule: the tool overrides a home OUTPUT_ROOT and names it in the record's overrides). The record `<OUTPUT_ROOT>/verification.json` holds job, scale, seconds, counts vs expected, exit codes and the package's file hashes before and after. Its `ok` means no failures; entity and edge count differences are never failures when the expected types show the run captures inputs or attributes. Do not touch the package after it: the validator compares those hashes with the published files.
- `repro_publish_traces` takes every count from the trace reader (the graph tools), never typed; a fact it cannot find reads `not recorded`. `run.sh` is the exact launch script copied from the run (it can hold the run's own paths); README, run.json and graph_summary.json have the home replaced by `${HOME}`. For an app whose package keeps no run script (BioTox) pass `run_sh=<the stage's launch script>`.
- `repro_publish_traces` indexes what it publishes: after the folder gets its final name it builds `raw/.dftindex` on all cores (an index is keyed by absolute file path, so the run's own `.dftindex` is never copied and a published folder is never moved), and gives the group the user's bits on it. Queries on the published run (a dataset opened from it) use that index directly and start at once. An index failure does not undo the publish: the result's `index.error` says so; run `repro_publish_index(publish_root, app, timestamps=<ts>)`. `repro_publish_index` also backfills runs published before this (app empty: every app).
- `repro_publish_permissions` runs `chgperm.sh --local -q -g moirai <dir>` on the published trees only, after the user agreed. NEVER run chgperm.sh by hand or on anything else; the tool refuses a script or folder outside publish_root. Afterwards it checks group, setgid directories, g=u, no other bits. Every published folder gets it, including the verification traces folder when it is published later.

## Publish a live run's data (the `publish_data` stage of a live pipeline)
- After the live run ended and `moirai_validate(pipeline, 'live_run')` passed: `repro_publish_traces(pipeline=<live pipeline>)`. It publishes into `<publish_root>/provenance-traces/<app>/<timestamp>/` with publish_root and app from the parent's `repro_publish_package`; give them when the parent never published.
- Then `repro_publish_data_validate` (it also requires `raw/.dftindex`), then the gate. Never publish while the run is still writing (the same_traces check fails). Both publish validators compare trace files by size and sha256 (seconds); pass `deep=true` only when the user asks for a full recount (minutes on a large run).

## Constraints
- No username, home path, session path or job id in any script. Use `${USER}` and `${HOME}` in `config.ini` (a lustre default keeps `${USER}`, never a literal name).
- Verification and its OUTPUT_ROOT are never under home: not the in-place OUTPUT_ROOT, not `isolation_root`, not a venv built for it. An explicit `output_root` argument wins over the default, but a home path is still refused.
- `isolation_root` and `in_place` are outside the session; `isolation_root` is on a file system compute nodes can see and is not under home.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs. A published package is never edited either: it is verified as it is, and fixed by a rebuild into a new target.
- Do not make the check pass by loosening `inputs/expected.json`.
- A run that captures run-dependent values has non-deterministic entity and edge counts: verify activities, types and dangling, not entity counts. Never compare entity or edge counts exactly for such a run, and never fail a verification on them; the activities and the entity types stay exact requirements.
- Batch verification submits one batch job (2 nodes exclusive, ~8 min, the baseline the same); ask before a longer limit or another node count. In-place verification is one smoke batch; the baseline is not rerun.
- Publishing writes only under publish_root and only new directories (plus the permission change on them).

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: wrap them with `set +u` in the loader.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A different number of activities means the app did different work: tell the user; do not hide it. A different number of entities or edges is expected when the annotation captures inputs and attributes (see Rules).
- A package built before this rule has an `expected.json` without `types`, so its published `prov_check.py` still compares counts exactly and fails on a correct run. It is fixed by a rebuild into a new target (`repro_build`, then publish again), never by editing the published files.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised. In batch mode give `reuse_venv` (app venv) and `moirai_venv` together; the full app environment build is never exercised by `repro_verify`.
- The app env needs `setup-env.sh` with the `emf_data` step removed, a login-node build (hours), and compute nodes cannot create files under any `python3.N/site-packages`: build once, then reuse.
- The batch time limit for `repro_verify` is `limit//60 - 2` minutes, so `flux cancel` fires before the job runner's own limit.
- The smoke activity count only matches when the smoke input is the same 2 lines: do not change `SMOKE_LINES`.
- A package published with `OUTPUT_ROOT=${HOME}/moirai_repro/${APP}` (elmerfold-v10 and older): `repro_verify(in_place)` ignores that home root, runs on lustre (`/p/lustre5/$USER/moirai_repro/<app>`, else `<session workspace parent>/verification/<app>`) via the env override, and writes the override into the record's `overrides`. Pass `output_root=` to choose another big file system.
- A failed venv build must not leave a half-built venv: `python3 -m venv` + pip that dies midway leaves a `moirai-venv` (41 MB on home, elmerfold_prov) that `02_setup_env.sh` then mistakes for a working one (`[[ ! -x $MPY ]]` is true once bin/python exists). Build into a temp name and rename on success, or remove the venv when pip fails; never leave the half-built one. Removing it needs the user's permission (the main thread's removal was denied once): report its path.
- The in-place run writes to OUTPUT_ROOT (lustre, see above): a second in-place run removes the earlier `verification.json` and traces under that root; publish the earlier run's traces first.
- chgperm adds `.chgperm_logs` inside the folders: hashes and checks skip it.
- `repro_verify(in_place)` may return before the batch ends (`wait`): call it again with the same in_place to complete the record; `repro_publish_traces(kind="verification")` needs it.
- A foreground tool call that sleeps about 590 s or more can be killed, and that cancels the verify batch (lesson L-766eca81): poll a long `repro_verify` in steps of 270 s or less and check `flux jobs <batch id>`; state CA with the log ending `Terminated` means rerun.
- A word starting with `f` and nine or more letters or digits in a script looks like a Flux job id to the lint (`repro_validate`): avoid such words in script text. A `grep -q` or `head` after a pipe under `set -o pipefail` can fail a script with SIGPIPE.
- A shared group directory: the publish tools create the parents; chgperm sets the final bits.

## Corrector
Predictors `repro_build`, `repro_verify`, `repro_publish_plan`, `repro_publish_package`, `repro_publish_traces`, `repro_publish_index`, `repro_publish_permissions`; validators `repro_validate`, `repro_batch_selftest`, `repro_publish_selftest`, `repro_check_selftest`, `repro_publish_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` (a published copy that fails: tell the user; republishing needs a new target) |
| `pinned` | install from a release or a git commit, so a version can be named |
| `no_login_service` | batch `04_run.sh` must not start a service: it belongs to the wrapper; fix `BATCH_STEPS` in the tool, then `repro_build` |
| `isolated_run`, rc 4 from `check_batch.py` | a chunk job failed: read the batch `--output` file, the chunk logs (`moirai-wrapper:` lines) and `service-<host>-<job>.log` in `traces/<scale>` |
| `isolated_run`, rc 5 | no wrapper ran: the generator did not take `--submit-script`, or the chunk jobs never started; read `run.sh` |
| `isolated_run`, rc 6 | the `#flux: --output` file is unreadable: wrong log dir, or the batch never started |
| `isolated_run`, other | the log names what the session gave and the package did not: usually a module, a path or an input |
| `repro_verify` / record: `Disk quota exceeded` (Errno 122) or status error naming the path and `df` | the OUTPUT_ROOT is on a full file system (home): rerun with `output_root=/p/lustre5/$USER/moirai_repro/<app>` (or a workspace folder); never free home by deleting the user's files unasked |
| `repro_verify`: isolation_root or output_root under home refused | give a lustre or workspace path |
| `reproduced`: `activities: N, the original run had M` | the app did different work: same input? same scale? a non-deterministic app needs the user's call |
| `reproduced`: `entity type T was not seen` | an annotation or the input no longer produces that entity type: compare with `expected.json` `types`; if the annotation changed on purpose, `repro_build` again |
| `reproduced`: `entities: N, the original run had M` or `edges: ...` | only an `expected.json` without `types` compares counts exactly: a run that captures run-dependent values has non-deterministic entity and edge counts, verify activities, types and dangling, not entity counts. Rebuild with `repro_build` (it stores the types) into a new target; do not loosen `expected.json` |
| plan: targets exist | publish under a new app/timestamp, or ask the user; never delete a published folder yourself |
| plan: `needs_input` | the baseline job start is unknown: pass `baseline_flux_job` (flux id) or `baseline_timestamp` |
| `published_package`, `run_folders_complete`, `published_runs` | the publish step named in the fix was skipped or failed halfway |
| `package_equals_source` | the published copy differs from final_provenance: rebuild and publish to a new target with the user's approval |
| `verification_record` | package hashes differ from the record (the package changed after the verified run) or the run failed: `repro_verify(in_place)` again |
| `readme_counts` | regenerate the run folder (new target) from the graph tools |
| `traces_as_counted` | a published trace file is not the file that was counted (size or sha256 differs from run.json's manifest): the run folder was changed after publishing, or the copy failed; publish the run again into a new folder. A folder published before manifests is checked by file count and bytes; `deep=true` recounts the graph |
| `same_traces` (publish_data) | the live run's files differ from the published copy: the run was still writing; publish again after it ends |
| `permissions` | the exceptions name each path; rerun `repro_publish_permissions`; if it persists, the script or the group is wrong (ask) |
| `indexed` | a run folder has no `raw/.dftindex`: `repro_publish_index(publish_root, app, timestamps=<ts>)`; if it fails again, the error names the file the indexer could not read |

## Self-heal
- `OSError: [Errno 122] Disk quota exceeded` on verification.json (seen 4 times on elmerfold-v10, /g/g92 at 24.0G of 24.0G): the OUTPUT_ROOT is on home. Do not retry; run `repro_verify(in_place, output_root=<lustre or workspace>)`. Check with `df -h <path>`; report the half-built `moirai-venv` (41 MB) left on home for the user to remove, do not delete it unasked.
- `moirai-service` fails only in the isolated run: the library path is set in `03_build.sh` (batch: `moirai_env.sh`) and `04_run.sh`; compare with the session's `moirai_env.sh`.
- `patch` fails: the app is not at the pinned state.
- Batch: `dbcast: command not found` in the chunk logs means `module load mpifileutils/0.11.1` did not happen in the submitting shell.
- Run `repro_batch_selftest`, `repro_publish_selftest` and `repro_check_selftest` after any change: they must print `PASS`.
- No baseline timestamp: the baseline job log did not name the flux id; ask the user for it rather than guess.
- A publish step died halfway: a `.tmp-<ts>` folder may remain under provenance-traces/<app>; report it, do not delete it unasked.

## What was verified and what was not
- Verified (sandbox, `repro_check_selftest`): the generated `prov_check.py` on traces made by the check. Two trace sets with the same entity types and different entity and edge counts pass (the note names the counts); extra types pass; a missing type fails; a different activity count fails; an `expected.json` without types, or with types that are not `.input`/`.attribute`, keeps the exact count comparison; the verification record keeps `ok` = no failures and drops entity and edge count differences when the types show run-dependent values.
- Verified (sandbox, `repro_batch_selftest`): the wrapper template renders and parses; the batch `04_run.sh` carries every generator flag, the module load, `flux batch`, `flux watch` and the check; `check_batch.py` exits 4 on the real failing run.out text (`flux-job: task(s) exited with exit code 2`, `<date>: <job> failed`), 0 on a clean one, 5 on empty or missing hosts, 6 on an unreadable output; the command parse; no literal user, workspace, session or job path in any rendered script.
- Verified (sandbox, `repro_publish_selftest`: temp publish_root, fake package and traces, fake chgperm.sh, the caller's own group as the fake group): the plan writes nothing and refuses; package copy and hash compare; traces copy and README/run.json/graph_summary.json from fake counts; refusals (existing target, outside publish_root, bad names, verification without record); permission application and verification (other bits, group, setgid); the validator catches a changed package and wrong README counts.
- Measured (the defect behind the types rule): two identical baseline runs gave 1079 and 1184 entities with the same 190 activities and entity types; the in-place smoke run gave 38 activities, 393 entities, 780 edges against an `expected.json` of 38/58/152 recorded from an older annotation.
- Measured (the home rule): 4 failed `repro_verify(in_place=<elmerfold-v10>)` calls, Errno 122 writing verification.json under `${HOME}/moirai_repro/elmerfold_prov`, home 24.0G of 24.0G.
- Not verified: the package has never run on its own from a published folder for Elmerfold. The wrapper approach and the `run.sh` flags were proven in real runs of the session; the package's generator call, `03_build.sh` rendering and the full flow under `env -i bash -l` were exercised only by the earlier isolated runs of the session copy. Untested for publishing with Elmerfold: `repro_verify(in_place)` end to end and its record; the flux id discovery from the baseline job log and `flux jobs -o {t_run}`; the README app commit, dftracer and moirai values come from the package pins (config.ini), not from the baseline run itself; `Env:` lists only MOIRAI_/DFTRACER_ exports of the wrapper. The full app environment build is never exercised. (BioTox published and verified in place: see [[workload_biotox_repro]].)

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated or in-place run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts (chgperm behaviour, flux fields, quota).
- A check that compares a number the annotation can change (entities, edges) is suspect: ask whether the number is deterministic before making it an exact requirement.
- A verification that failed on a full file system is a site fact: the user said verification belongs on lustre or workspace, never home; `lesson_add` any new file system that fills up.
