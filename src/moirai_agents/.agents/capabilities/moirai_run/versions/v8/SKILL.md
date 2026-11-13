---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; also apps launched by a generated `flux batch` run.sh (Elmerfold): the app job runs a generated per-chunk submit wrapper (submit_moirai.sh, given to run.sh as --submit-script, written beforehand by moirai_wrapper_write) that starts moirai-service inside the job's own shell; dry-run first. Load for the moirai_smoke and baseline stages, for 'flux batch', 'run.sh', 'submit-script', 'Submit script ... does not exist', 'wrapper_write', 'bulksubmit', 'drain', 'exclusive', 'dftracer_service not found', 'no dftracer_service found', 'exited with exit code', 'failed' in run.out, 'false pass', 'spill-only', 'No running server found'.
---

Cross-references: [[tools_moirai]] [[workload_elmerfold]] [[flux-alloc]] [[bug-dftracer-service-start-blocks-flux-run]] [[bug-dftracer-service-hosts-must-be-pinned]] [[app_smoke]]

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.
- Order of use for batch mode (an app launched by a generated `flux batch` run.sh, e.g. Elmerfold; run.sh stays UNMODIFIED):
  1. `moirai_wrapper_write(pipeline, stage, service_venv)`: writes ONLY `<work>/traces/<stage>/submit_moirai.sh` (mode 755; creates the stage and raw dirs; returns the path and the sha256; the same text again changes nothing). It submits and starts nothing. `service_venv` defaults to the venv root of the integrate stage's `moirai.python` and is verified to hold an executable `dftracer/bin/dftracer_service`; the text is identical whether it is passed or defaulted.
  2. `elmerfold_run_script(..., submit_script=<that path>)` (and `cwd`, see `workload_elmerfold`): the generator requires the submit script to exist.
  3. `elmerfold_run_check(..., submit_script=<that path>)`.
  4. `moirai_batch_plan(stage, batch_script=<run.sh>)`: checks run.sh, shows the exact submit command, the `--output` file the batch wrapper job will read (`batch_output`) and whether the wrapper on disk equals the fresh render (`wrapper_on_disk`).
  5. The user's yes.
  6. `moirai_smoke_run` / `moirai_baseline_run` with `batch_script`, `queue` (default pdebug), `limit` (seconds; batch gets ceil(limit/60) minutes), and the SAME `service_venv`/`original_script`/`data_dir` as in step 1.
- Before submitting, `moirai_smoke_run`/`moirai_baseline_run` compare the wrapper on disk with the freshly rendered text. When they differ (a different `service_venv`, edited by hand, an older render) they REWRITE it and say so (`wrapper.action` = `rewritten`, with both sha256) in the result and the stage note. They still refuse a run.sh that does not reference the wrapper, before writing or submitting anything.
- The wrapper `<work>/traces/<stage>/submit_moirai.sh` is what `flux bulksubmit` runs on the exclusive worker node, once per chunk: `submit_moirai.sh <chunk csv>`. It sources `moirai_env.sh`; exports `MOIRAI_LOG_FILE=<stage>/raw/prov`, `MOIRAI_DATA_DIR`, `MOIRAI_SERVICE_SHM=<base>-${FLUX_JOB_ID:-$$}` (unique per job), `MOIRAI_PC_BLOCKS` (16) and `MOIRAI_PC_FLUSH_MS` (250); resolves `dftracer_service` (see below); appends its host to `<stage>/hosts.txt`; starts `moirai-service start` IN THE BACKGROUND of its own shell (output to `<stage>/service-<host>-<job>.log`); waits up to 60 s for `/dev/shm$MOIRAI_SERVICE_SHM`; on failure exits 3 with a message BEFORE the app (no silent spill); traps EXIT/TERM/INT to run `moirai-service stop` with the same env; runs `bash <original submit_smsp.sh> "$@"` as a child, keeps its rc, stops the service, exits with the rc. The original is the ANNOTATED copy's `monomer-structure-prediction/submit_smsp.sh` unless `original_script` is given.
- dftracer_service resolution in the wrapper, in order: an explicit `MOIRAI_DFTRACER_SERVICE` in the environment wins; else the build-time default printed by `moirai-service --help` when it exists; else the baked resolved path, then `<dftracer package dir>/bin/dftracer_service` where the package dir comes from `python -c 'import dftracer,os;print(os.path.dirname(dftracer.__file__))'` (python on PATH after sourcing `moirai_env.sh` is the venv's), then `<service_venv>/lib/python*/site-packages/dftracer/bin/dftracer_service`. The venv's `moirai-service` is a Python entry-point script: its location says nothing about where dftracer lives.
- The batch wrapper job (detached, `jobs.start`) loads `mpifileutils/0.11.1`, checks `dbcast`, runs `flux batch -t <min> --queue <q> <run.sh>`, `flux watch <id>`, reads `{returncode}` and `{result}`, exits non-zero with a clear message unless COMPLETED and 0. THEN it reads the batch's `--output` file (the path in run.sh's `#flux: --output <path>` header, parsed at render time) because the batch return code does not reflect its bulksubmit chunk jobs: exit 4 naming the failed chunk job id(s) when the file has `exited with exit code <non-zero>` or a line ending ` failed`; exit 5 when `<stage>/hosts.txt` is missing or empty (no per-chunk wrapper ran); exit 6 when the output file is not readable. `dbcast` must be on PATH of the submitting shell: flux batch propagates the environment.
- Exact submit command shape: `flux batch -t <ceil(limit/60)> --queue <queue> <run.sh>` (40 min for the 2400 s smoke), run from that wrapper job. run.sh is never rewritten; the service is never started with `flux run`/`flux submit`.
- `moirai_batch_plan` validates run.sh: exactly one `flux bulksubmit` line, a `:::` input list, the token before `'{}'` is the wrapper path, the annotated tree path appears in run.sh, no old moirai preamble; it warns when run.sh has no `#flux: --output` header (then only hosts.txt is checked).

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. The tools submit nothing but the wrapper job's one `flux batch`; `moirai_wrapper_write` writes one file and nothing else.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (2 nodes + Rabbit, 40 min for the Elmerfold smoke).
- Never start moirai-service as a flux job in this app: after the drain, prestage, the dbcast steps and the app job each need the whole worker node (`--exclusive`, `--nodes $num_nodes --tasks-per-node 1`), so a one-core service job blocks them until the limit. Never `dftracer_service start` under `flux run` either: it never returns.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each job has its own ring.
- Do not edit run.sh or the wrapper by hand: regenerate (`moirai_wrapper_write`).
- A batch job's return code is not evidence that the app ran. Judge a batch run by its `--output` file, `hosts.txt`, the service logs and the traces, never by `COMPLETED`/rc 0 alone.

## Corner cases
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: the app did not reach the ring. `moirai_validate` names the hosts that only have spill traces.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit` (the wrapper job has limit+300 s; the batch has `-t`).
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` shows a default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (gone). The real one is `<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service`; the compiled `site-packages/moirai/bin/moirai-service` (not the venv's `bin/moirai-service` entry script) holds `../../dftracer/bin/dftracer_service`. `moirai_service_resolve(venv=...)` finds it and reports `binary_kind`. Pass `service_venv` or let it default.
- Wrapper exit codes: 2 setup problem (original script, env file, moirai-service or dftracer_service missing), 3 ring not visible, 130/143 INT/TERM, otherwise the app's rc. Batch wrapper job exit codes: the batch's own rc when it failed, 4 a chunk job failed, 5 no host ran the wrapper, 6 `--output` unreadable.
- The wrapper text depends on `service_venv`, `data_dir` and `original_script`: a different value at run time than at `moirai_wrapper_write` makes the run rewrite the file (reported). A defaulted `service_venv` renders the same text as the explicit one. The v7 fallback change (python-based resolver) changes the text once: a wrapper written by v6 is reported `rewritten` at the next run.
- `--output` is truncated and rewritten per run, so the file only describes the last run.

## Failure 1 (D1): false pass, the batch returned 0 while the chunk job failed
(confirmed 2026-10-30, pipeline elmerfold-prov-20261030-125944, batch job f3hFu78obrzb, wrapper job moirai-smoke-batch-934280540)
- Symptom: the per-chunk wrapper exited 2 (`moirai-wrapper: no dftracer_service found`), the app never ran, yet the batch finished COMPLETED returncode 0 and the wrapper job `moirai-smoke-batch` exited 0. `run.out` (path in `#flux: --output`, here `/p/lustre5/haridev/elmerfold/runs/moirai-smoke/run.out`) held `flux-job: task(s) exited with exit code 2`, `Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed`, `flux-job: there are no more waitable jobs`, `All jobs completed (or failed)!`. A good run has none of the first two.
- Cause: run.sh ends normally even when its `flux bulksubmit` chunk job fails, so the batch rc is 0.
- Fix: the batch wrapper job reads `--output` after `flux watch` and fails with exit 4 (message names the chunk job id, here f35QqToh9), 5 or 6 (see Rules). Verify: `moirai_wrapper_selftest` shows `batch_fail_detected=ok batch_clean_pass=ok batch_hosts_missing=ok`; those run the rendered job text under bash with a fake flux against this exact failing text and a clean one.

## Failure 2 (D2): wrong dftracer_service resolver
(confirmed 2026-10-30, same run)
- Symptom: `moirai-wrapper: no dftracer_service found (default ... missing)`, exit 2, and the warning `no service venv given and dftracer_service not resolved here`.
- Cause: the fallback resolved `$(dirname "$(readlink -f "$(command -v moirai-service)")")/../../dftracer/bin/dftracer_service`, but `/p/lustre5/haridev/elmerfold/moirai-venv/bin/moirai-service` is a Python entry-point script (`#!.../bin/python`, `from moirai.service import main`), not the compiled binary (`site-packages/moirai/bin/moirai-service`); the real service is `site-packages/dftracer/bin/dftracer_service`.
- Fix: `service_venv` defaults from integrate's `moirai.python` (venv root) and `moirai_wrapper_write`/`moirai_smoke_run`/`moirai_baseline_run`/`moirai_batch_plan` raise an error naming the fix when `<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service` is missing or not executable; the wrapper fallback resolves the package through python. Verify: `moirai_service_resolve(venv=<venv>)` returns `path` and `exists: true`; `ls -l <venv>/lib/python*/site-packages/dftracer/bin/dftracer_service`.

## Unconfirmed (state it to the user; nothing below was run)
- Whether `moirai-service start` returns by itself (daemonizes) or stays in the foreground: neither `--help` nor a start was run. The wrapper does not depend on it (background plus ring wait; stop then reaps the pid, kill after 10 s).
- libstdc++ shadowing: `submit_smsp.sh` puts `${ELMER_ENV}/lib` first in `LD_LIBRARY_PATH` (line 110) AFTER the wrapper started the service, so the service is not affected, but the app process loading moirai/dftracer libs may pick the conda libstdc++. Symptom: `GLIBCXX_... not found` in the chunk log. Not tested.
- Trace file names per host: assumed to carry the host name; if not, `host_traces` is inconclusive.
- Whether the GPU inference server runs on the same node as the driver (the one running the wrapper): a server on another node is not traced by this service.
- `flux bulksubmit` environment: assumed to pass run.sh's `ELMER_*` to the wrapper (the original needs them, so the app job already relies on it).
- `moirai_wrapper_write` and the rewrite path were tested only on temp dirs through the pure functions, not on a real pipeline.
- `flux jobs -o {returncode}`/`{result}` and `flux batch -t <min>` are taken from memory, not run here; the batch wrapper job was run only against a fake flux.
- That `python` on PATH after sourcing `moirai_env.sh` is the venv shim, and that the integrate outputs key is `moirai.python` (flat or nested are both accepted): not run on a real pipeline.
- Other failure lines a real chunk failure can leave in `--output` (beyond the two seen) are not known; a failure that leaves neither pattern still shows as no host / no trace in `moirai_validate`.

## Corrector
Predictor `moirai_wrapper_write`, `moirai_smoke_run`, `moirai_baseline_run`, `moirai_batch_plan`, `moirai_batch_render`, `moirai_service_resolve`, validator `moirai_validate` (and `moirai_wrapper_selftest` for the wrapper tools). A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval; batch: the wrapper line `batch job ... FAILED`, then the batch `--output` file and the chunk log in `test_logs` (wrapper messages start `moirai-wrapper:`) |
| `exit_0` with rc 4 (`returned 0 but a chunk job FAILED`) | D1: the batch was COMPLETED but a bulksubmit chunk job failed; read the named chunk job's log and the `moirai-wrapper:` line in `--output`/`test_logs`; fix that cause (for exit 2: D2 row), then rerun. Do not treat the batch rc as success |
| `exit_0` with rc 5 (`left no host in hosts.txt`) | no per-chunk wrapper ran: run.sh did not reach the bulksubmit line or the wrapper path in it is wrong; `moirai_batch_plan` with `batch_script`, read `--output` |
| `exit_0` with rc 6 (`--output file ... not readable`) | the `#flux: --output` path is wrong or not shared with the wrapper job's node; read the path from run.sh and `moirai_batch_plan` `batch_output` |
| `trace`, `service_trace` | read `service-<host>-<job>.log` next to the traces; `/dev/shm`, ring size |
| `host_traces` | a host listed in `hosts.txt` has no trace: its service did not start or file names lack the host; read `service-<host>-<job>.log` |
| `host_service_trace` | only spill traces: the app did not see the ring; same `MOIRAI_SERVICE_SHM` in the app env (the wrapper exports it before the child), `/dev/shm` on that host |
| `activities`, `types_seen` | the annotated path did not run: back to the spec or an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |
| plan: "does not reference the wrapper" | regenerate run.sh with `--submit-script <wrapper path>` printed by `moirai_wrapper_write`; do not edit run.sh |
| plan: "annotated tree" | run.sh was generated from the original tree: regenerate from the annotated copy |
| plan: "no ':::' input list" / "expected exactly one flux bulksubmit" | run.sh changed shape: update `check_run_sh` (`capability_update`) after reading it |
| plan warning: "no '#flux: --output' header" | run.sh does not name its output file: regenerate it, or extend `batch_output_path` (`capability_update`) for the new header shape |
| generator: "Submit script ... does not exist" | run `moirai_wrapper_write` for the stage first, then regenerate run.sh |
| run result `wrapper.action` = `rewritten` | the wrapper differed from the fresh render; tell the user; pass the same `service_venv`/`data_dir`/`original_script` as in `moirai_wrapper_write` to avoid it |
| `moirai-wrapper: ... no dftracer_service` (exit 2) / error "service venv ... holds no executable dftracer_service" | D2: `moirai_service_resolve(venv=<venv root of integrate's moirai.python>)`; pass the venv where dftracer is installed as `service_venv`, install dftracer there, or export `MOIRAI_DFTRACER_SERVICE`; the message shows whether the dftracer package was importable |
| `moirai-wrapper: ring ... not visible` (exit 3) | read the service log; ring size; does `moirai-service start` work on that node |
| `GLIBCXX` in a chunk log | libstdc++ shadowing (see Unconfirmed): add the venv lib dir first for the app in the wrapper, via `capability_update` |

## Self-heal
- `exit_0` failed: compare `log_tail` with the smoke stage; failures with `moirai` in them are annotation or link problems.
- `activities` is zero: the app ran from somewhere other than the annotated tree (run.sh `ELMER_SRC_DIR`), or the annotated functions are not reached.
- Service does not start: read the service log (`No space left on device` means the ring).
- `No running server found` on stop: start and stop used different env; the wrapper stops with the env it started with, so check the service log for the host.
- A run that hangs at `Prestage` or `dbcast`: something holds a core on the worker node; `moirai_run_stop` and look for a stray service job (`flux jobs -a`): none should exist in this design.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules. Elmerfold's run.sh shape, once run, belongs in `workload_elmerfold`.
- Lesson (2026-10-30, pipeline elmerfold-prov-20261030-125944): the v3 design started moirai-service as pinned `flux submit` jobs after the drain and rewrote run.sh. Dropped because, in the real generated run.sh, the prestage `flux run --nodes $num_nodes --tasks-per-node 1`, the `flux run --exclusive ... dbcast` steps and `flux bulksubmit --nodes 1 --exclusive` all need the whole worker node; a one-core service job there would block them until the limit. The service now lives inside the app job (the wrapper).
- Lesson (2026-10-30): a tool that creates an input another generator demands must be callable before that generator (wrapper_write before run.sh), not only as a side effect of submitting.
- Lesson (2026-10-30, batch job f3hFu78obrzb): a batch job's return code does not reflect its `flux bulksubmit` chunk jobs, because run.sh ends normally when a chunk job fails. A COMPLETED rc 0 batch proves nothing; read its `--output` file (failed chunk lines), `hosts.txt` and the traces.
- Lesson (2026-10-30): a pip venv's `bin/<tool>` is usually a Python entry-point script; derive sibling-package paths from the installed package (`import dftracer`), not from the script's location.
- Run settings that worked for this app: `lesson_add`. Settle each Unconfirmed item with the run that proves it (`(confirmed YYYY-MM-DD)`).
- A failure mode the validator missed: `capability_update` with a check for it.
