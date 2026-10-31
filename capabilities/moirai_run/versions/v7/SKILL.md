---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; also apps launched by a generated `flux batch` run.sh (Elmerfold): the app job runs a generated per-chunk submit wrapper (submit_moirai.sh, given to run.sh as --submit-script, written beforehand by moirai_wrapper_write) that starts moirai-service inside the job's own shell; the batch wrapper job fails when a bulksubmit chunk job failed; dry-run first. Load for the moirai_smoke and baseline stages, for 'flux batch', 'run.sh', 'submit-script', 'Submit script ... does not exist', 'wrapper_write', 'bulksubmit', 'drain', 'exclusive', 'dftracer_service not found', 'no dftracer_service found', 'exited with exit code', 'false pass', 'spill-only', 'No running server found'.
---

Cross-references: [[tools_moirai]] [[workload_elmerfold]] [[flux-alloc]] [[bug-dftracer-service-start-blocks-flux-run]] [[bug-dftracer-service-hosts-must-be-pinned]] [[app_smoke]]

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.
- Order of use for batch mode (an app launched by a generated `flux batch` run.sh, e.g. Elmerfold; run.sh stays UNMODIFIED):
  1. `moirai_wrapper_write(pipeline, stage, service_venv)`: writes ONLY `<work>/traces/<stage>/submit_moirai.sh` (mode 755; creates the stage and raw dirs; returns the path and the sha256; the same text again changes nothing). It submits and starts nothing. `service_venv` defaults to the venv of the moirai python in the integrate outputs (`moirai.python`); the write refuses (error naming the fix) when `<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service` is not an executable file. The result shows `service_venv`, its source and `dftracer_service`.
  2. `elmerfold_run_script(..., submit_script=<that path>)` (and `cwd`, see `workload_elmerfold`): the generator requires the submit script to exist.
  3. `elmerfold_run_check(..., submit_script=<that path>)`.
  4. `moirai_batch_plan(stage, batch_script=<run.sh>)`: checks run.sh, shows the exact submit command, whether the wrapper on disk equals the fresh render (`wrapper_on_disk`) and the batch `--output` file that will be read (`batch_output`).
  5. The user's yes.
  6. `moirai_smoke_run` / `moirai_baseline_run` with `batch_script`, `queue` (default pdebug), `limit` (seconds; batch gets ceil(limit/60) minutes), and the SAME `service_venv`/`original_script`/`data_dir` as in step 1.
- Before submitting, `moirai_smoke_run`/`moirai_baseline_run` compare the wrapper on disk with the freshly rendered text. When they differ (a different `service_venv`, edited by hand, an older render) they REWRITE it and say so (`wrapper.action` = `rewritten`, with both sha256) in the result and the stage note. They still refuse a run.sh that does not reference the wrapper, before writing or submitting anything.
- The wrapper `<work>/traces/<stage>/submit_moirai.sh` is what `flux bulksubmit` runs on the exclusive worker node, once per chunk: `submit_moirai.sh <chunk csv>`. It sources `moirai_env.sh`; exports `MOIRAI_LOG_FILE=<stage>/raw/prov`, `MOIRAI_DATA_DIR`, `MOIRAI_SERVICE_SHM=<base>-${FLUX_JOB_ID:-$$}` (unique per job), `MOIRAI_PC_BLOCKS` (16) and `MOIRAI_PC_FLUSH_MS` (250); finds `dftracer_service` (order: an explicit `MOIRAI_DFTRACER_SERVICE`; the build-time default of `moirai-service --help` when it exists; else the package of the python on PATH, `python -c 'import dftracer,os;print(os.path.dirname(dftracer.__file__))'` then `<pkg>/bin/dftracer_service`, plus the resolved path and the venv glob baked in at render time) and exports `MOIRAI_DFTRACER_SERVICE`; appends its host to `<stage>/hosts.txt`; starts `moirai-service start` IN THE BACKGROUND of its own shell (output to `<stage>/service-<host>-<job>.log`); waits up to 60 s for `/dev/shm$MOIRAI_SERVICE_SHM`; on failure exits 3 with a message BEFORE the app (no silent spill); traps EXIT/TERM/INT to run `moirai-service stop` with the same env; runs `bash <original submit_smsp.sh> "$@"` as a child, keeps its rc, stops the service, exits with the rc. The original is the ANNOTATED copy's `monomer-structure-prediction/submit_smsp.sh` unless `original_script` is given.
- The batch wrapper job (detached, `jobs.start`) loads `mpifileutils/0.11.1`, checks `dbcast`, runs `flux batch -t <min> --queue <q> <run.sh>`, `flux watch <id>`, reads `{returncode}` and `{result}`, exits non-zero with a clear message unless COMPLETED and 0. THEN it verifies what the batch return code cannot show (below). `dbcast` must be on PATH of the submitting shell: flux batch propagates the environment.
- Batch wrapper job exit codes, distinct: 1 batch submission/module failure, the batch's own rc when non-zero, 20 a bulksubmit chunk job failed (the batch `--output` file has `exited with exit code <non-zero>` or a line ending ` failed`; the message names the chunk job id and the log), 21 `hosts.txt` missing (no per-chunk wrapper ran), 22 `hosts.txt` empty, 23 the batch `--output` file does not exist. A run.sh without a `#flux: --output` line only warns (chunk failures cannot be seen). The `--output` path is parsed from run.sh's `#flux: --output <path>` line at render time. `moirai_batch_verify_test` runs this exact snippet with bash on a given log text (no cluster).
- Exact submit command shape: `flux batch -t <ceil(limit/60)> --queue <queue> <run.sh>` (40 min for the 2400 s smoke), run from that wrapper job. run.sh is never rewritten; the service is never started with `flux run`/`flux submit`.
- `moirai_batch_plan` validates run.sh: exactly one `flux bulksubmit` line, a `:::` input list, the token before `'{}'` is the wrapper path, the annotated tree path appears in run.sh, no old moirai preamble.

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. The tools submit nothing but the wrapper job's one `flux batch`; `moirai_wrapper_write` writes one file and nothing else.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (2 nodes + Rabbit, 40 min for the Elmerfold smoke).
- Never start moirai-service as a flux job in this app: after the drain, prestage, the dbcast steps and the app job each need the whole worker node (`--exclusive`, `--nodes $num_nodes --tasks-per-node 1`), so a one-core service job there blocks them until the limit. Never `dftracer_service start` under `flux run` either: it never returns.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each job has its own ring.
- Do not edit run.sh or the wrapper by hand: regenerate (`moirai_wrapper_write`).
- Never read the stage as passed from the batch job's return code or from `exit_0` alone: confirm the wrapper job's verification line (`verified: no failed chunk job, hosts recorded`) and `moirai_validate`.

## Corner cases
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: the app did not reach the ring. `moirai_validate` names the hosts that only have spill traces.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit` (the wrapper job has limit+300 s; the batch has `-t`).
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` shows a default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (gone). `moirai-service` in the venv is a Python entry-point script (`#!<venv>/bin/python; from moirai.service import main`), NOT the compiled binary (that is `site-packages/moirai/bin/moirai-service`), so a path relative to the script is wrong. The real service is `<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service`. `moirai_service_resolve` takes the venv from its argument or the script's shebang; the wrapper exports `MOIRAI_DFTRACER_SERVICE` and exits 2 with a clear line when nothing exists.
- Wrapper exit codes: 2 setup problem (original script, env file, moirai-service or dftracer_service missing), 3 ring not visible, 130/143 INT/TERM, otherwise the app's rc.
- The wrapper text depends on `service_venv`, `data_dir` and `original_script`: a different value at run time than at `moirai_wrapper_write` makes the run rewrite the file (reported), so run.sh keeps working but the file changed after you looked at it.

## Submit script must exist; job cwd follows it
(confirmed 2026-10-30 by reading `utils/gen_launch_script.py` of the Elmerfold checkout, pipeline elmerfold-prov-20261030-125944; not run)
- Lines 849-850: `--submit-script` must be an EXISTING file (error `Submit script ... does not exist`). The wrapper used to be written only when a run was submitted, so `elmerfold_run_script --submit-script <wrapper>` could not work. Fix: `moirai_wrapper_write` first (Order of use, step 1). Verify: `moirai_wrapper_selftest` line says PASS; `ls -l <work>/traces/<stage>/submit_moirai.sh` shows mode 755.
- Line 1076: without `--cwd` the generator sets the job cwd to the submit script's PARENT directory. With the wrapper under `<work>/traces/<stage>/` the app job would start there, not in `<annotated>/monomer-structure-prediction` (the earlier real run.sh had `#flux: --cwd <annotated>/monomer-structure-prediction`). Fix: `elmerfold_run_script` passes `--cwd` (see `workload_elmerfold`); `elmerfold_run_check` fails otherwise.

## Batch job return code does not reflect its chunk jobs
### `moirai-wrapper: no dftracer_service found` in a chunk, yet the batch is COMPLETED rc 0 (false pass)
(confirmed 2026-10-30, batch job f3hFu78obrzb, wrapper job moirai-smoke-batch-934280540, pipeline elmerfold-prov-20261030-125944)
- Evidence: the per-chunk wrapper exited 2; `/p/lustre5/haridev/elmerfold/runs/moirai-smoke/run.out` (the `#flux: --output` file) held `flux-job: task(s) exited with exit code 2`, `Fri Oct 30 23:32:42 PDT 2026: f35QqToh9 failed`, `flux-job: there are no more waitable jobs`, `All jobs completed (or failed)!`. run.sh ends normally even when its `flux bulksubmit` chunk job fails, so the batch job finished COMPLETED with returncode 0, the wrapper job exited 0 and the stage output looked like success; only `moirai_validate` caught it (trace, activities, entities failed).
- Cause: a batch job's return code is run.sh's last command, not its bulksubmit chunk jobs.
- Fix (v7): the batch wrapper job reads the `--output` file after `flux watch` and exits 20 (chunk job id and log named), 21/22 for a missing/empty `hosts.txt`, 23 for a missing log. Verify: `moirai_batch_verify_test` with the failed text gives rc 20 and `chunk_job_id` f35QqToh9, with a clean text rc 0; `moirai_wrapper_selftest` shows `batch_output_fail=ok`.
### `no dftracer_service found` with a `moirai-service` that is a Python script
(confirmed 2026-10-30, same run)
- Cause: the wrapper's fallback resolved `$(dirname "$(readlink -f "$(command -v moirai-service)")")/../../dftracer/bin/dftracer_service`. `/p/lustre5/haridev/elmerfold/moirai-venv/bin/moirai-service` is an entry-point script, so that path is wrong.
- Fix (v7): `service_venv` defaults from the integrate outputs and is verified at write time; the in-wrapper fallback resolves the package through python (`import dftracer`) then `<pkg>/bin/dftracer_service`; an explicit `MOIRAI_DFTRACER_SERVICE` still wins. Verify: `moirai_service_resolve(binary=<venv>/bin/moirai-service)` returns the site-packages path; `moirai_wrapper_selftest` shows `venv_from_shebang=ok` and `wrapper_python_resolver=ok`; the 'no service venv given' warning is gone.

## Unconfirmed (state it to the user; nothing below was run)
- Whether `moirai-service start` returns by itself (daemonizes) or stays in the foreground: neither `--help` nor a start was run. The wrapper does not depend on it (background plus ring wait; stop then reaps the pid, kill after 10 s).
- libstdc++ shadowing: `submit_smsp.sh` puts `${ELMER_ENV}/lib` first in `LD_LIBRARY_PATH` (line 110) AFTER the wrapper started the service, so the service is not affected, but the app process loading moirai/dftracer libs may pick the conda libstdc++. Symptom: `GLIBCXX_... not found` in the chunk log. Not tested.
- Trace file names per host: assumed to carry the host name; if not, `host_traces` is inconclusive.
- Whether the GPU inference server runs on the same node as the driver (the one running the wrapper): a server on another node is not traced by this service.
- `flux bulksubmit` environment: assumed to pass run.sh's `ELMER_*` to the wrapper (the original needs them, so the app job already relies on it).
- The v7 batch verification was run with bash on temp files and the real failed run.out text, not in a real batch; the shape of a CLEAN run.out is synthetic (no clean real run exists yet). A real chunk failure that prints neither pattern would still pass; `moirai_validate` is the second line.
- That the `python` on PATH after sourcing `moirai_env.sh` imports `dftracer` on the compute node, and that `moirai.python` is the key name in the integrate outputs (both forms `moirai: {python}` and `moirai.python` are read): not run against the real pipeline.
- `flux jobs -o {returncode}`/`{result}` and `flux batch -t <min>` are taken from memory, not run here (the 2026-10-30 submission did use them and finished COMPLETED rc 0).

## Corrector
Predictor `moirai_wrapper_write`, `moirai_smoke_run`, `moirai_baseline_run`, `moirai_batch_plan`, `moirai_batch_render`, `moirai_service_resolve`, validator `moirai_validate` (and `moirai_wrapper_selftest`, `moirai_batch_verify_test` for the wrapper tools). A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval; batch: the wrapper line `batch job ... FAILED`, then the batch `--output` file and the chunk log in `test_logs` (wrapper messages start `moirai-wrapper:`) |
| `exit_0` rc 20 (chunk job failed) | the wrapper job names the chunk job id and the log; read that chunk's log (`moirai-wrapper:` lines) and `service-<host>-<job>.log`; fix the cause (D1: a batch rc 0 hides it), then rerun |
| `exit_0` rc 21 / 22 | `hosts.txt` missing or empty: no per-chunk wrapper ran; was run.sh generated with `--submit-script <wrapper>`? `moirai_batch_plan` checks it; also the chunk log for `moirai-wrapper:` setup errors (exit 2) |
| `exit_0` rc 23 | the batch `--output` file does not exist: path of `#flux: --output` in run.sh (wrong or not writable); fix run.sh generation |
| `trace`, `service_trace` | read `service-<host>-<job>.log` next to the traces; `/dev/shm`, ring size |
| `host_traces` | a host listed in `hosts.txt` has no trace: its service did not start or file names lack the host; read `service-<host>-<job>.log` |
| `host_service_trace` | only spill traces: the app did not see the ring; same `MOIRAI_SERVICE_SHM` in the app env (the wrapper exports it before the child), `/dev/shm` on that host |
| `activities`, `types_seen` | the annotated path did not run: back to the spec or an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |
| plan: "does not reference the wrapper" | regenerate run.sh with `--submit-script <wrapper path>` printed by `moirai_wrapper_write`; do not edit run.sh |
| plan: "annotated tree" | run.sh was generated from the original tree: regenerate from the annotated copy |
| plan: "no ':::' input list" / "expected exactly one flux bulksubmit" | run.sh changed shape: update `check_run_sh` (`capability_update`) after reading it |
| generator: "Submit script ... does not exist" | run `moirai_wrapper_write` for the stage first, then regenerate run.sh |
| run result `wrapper.action` = `rewritten` | the wrapper differed from the fresh render; tell the user; pass the same `service_venv`/`data_dir`/`original_script` as in `moirai_wrapper_write` to avoid it |
| write/run: "dftracer_service not found: looked in ..." | pass `service_venv=<venv root of the moirai python>` with dftracer installed (`<venv>/lib/python*/site-packages/dftracer/bin/dftracer_service` executable), or re-run `integrate_apply`; `moirai_service_resolve(venv=...)` shows what was tried |
| `moirai-wrapper: ... no dftracer_service` (exit 2) | the message prints the python and dftracer package it found; `moirai_service_resolve` with the venv; install dftracer there or export `MOIRAI_DFTRACER_SERVICE`; never a path relative to the `moirai-service` script |
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
- Lesson (2026-10-30, batch f3hFu78obrzb): a batch job's return code does not reflect its `flux bulksubmit` chunk jobs; verify the batch `--output` file and `hosts.txt` after `flux watch`. Never infer a launcher's success from the rc of a script that only submits.
- Lesson (2026-10-30): never derive a package path from the path of an entry-point script; resolve the package through the interpreter (`import <pkg>`).
- Run settings that worked for this app: `lesson_add`. Settle each Unconfirmed item with the run that proves it (`(confirmed YYYY-MM-DD)`).
- A failure mode the validator missed: `capability_update` with a check for it.
