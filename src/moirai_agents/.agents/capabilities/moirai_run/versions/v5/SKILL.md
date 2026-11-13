---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; also apps launched by a generated `flux batch` run.sh (Elmerfold): the app job runs a generated per-chunk submit wrapper (submit_moirai.sh, given to run.sh as --submit-script) that starts moirai-service inside the job's own shell; dry-run first. Load for the moirai_smoke and baseline stages, for 'flux batch', 'run.sh', 'submit-script', 'bulksubmit', 'drain', 'exclusive', 'dftracer_service not found', 'spill-only', 'No running server found'.
---

Cross-references: [[tools_moirai]] [[workload_elmerfold]] [[flux-alloc]] [[bug-dftracer-service-start-blocks-flux-run]] [[bug-dftracer-service-hosts-must-be-pinned]] [[app_smoke]]

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.
- Batch mode (an app launched by a generated `flux batch` run.sh, e.g. Elmerfold) works with the UNMODIFIED run.sh. Order: (1) `moirai_batch_plan` for the stage: it prints the wrapper path and text; (2) generate run.sh with `elmerfold_run_script ... --submit-script <wrapper path>` from the annotated tree; (3) `moirai_batch_plan` again with `batch_script=<run.sh>`: it checks run.sh and prints the exact submit command; (4) the user's yes; (5) `moirai_smoke_run` / `moirai_baseline_run` with `batch_script`, `queue` (default pdebug), `limit` (seconds; batch gets ceil(limit/60) minutes).
- The wrapper `<work>/traces/<stage>/submit_moirai.sh` is what `flux bulksubmit` runs on the exclusive worker node, once per chunk: `submit_moirai.sh <chunk csv>`. It sources `moirai_env.sh`; exports `MOIRAI_LOG_FILE=<stage>/raw/prov`, `MOIRAI_DATA_DIR`, `MOIRAI_SERVICE_SHM=<base>-${FLUX_JOB_ID:-$$}` (unique per job), `MOIRAI_PC_BLOCKS` (16) and `MOIRAI_PC_FLUSH_MS` (250); exports `MOIRAI_DFTRACER_SERVICE` when the build-time default is missing; appends its host to `<stage>/hosts.txt`; starts `moirai-service start` IN THE BACKGROUND of its own shell (output to `<stage>/service-<host>-<job>.log`); waits up to 60 s for `/dev/shm$MOIRAI_SERVICE_SHM`; on failure exits 3 with a message BEFORE the app (no silent spill); traps EXIT/TERM/INT to run `moirai-service stop` with the same env; runs `bash <original submit_smsp.sh> "$@"` as a child, keeps its rc, stops the service, exits with the rc.
- The batch wrapper job (detached, `jobs.start`) loads `mpifileutils/0.11.1`, checks `dbcast`, runs `flux batch -t <min> --queue <q> <run.sh>`, `flux watch <id>`, reads `{returncode}` and `{result}`, exits non-zero with a clear message unless COMPLETED and 0. `dbcast` must be on PATH of the submitting shell: flux batch propagates the environment.
- Exact submit command shape: `flux batch -t <ceil(limit/60)> --queue <queue> <run.sh>` (40 min for the 2400 s smoke), run from that wrapper job. run.sh is never rewritten; the service is never started with `flux run`/`flux submit`.
- `moirai_batch_plan` validates run.sh: exactly one `flux bulksubmit` line, a `:::` input list, the token before `'{}'` is the wrapper path, the annotated tree path appears in run.sh, no old moirai preamble.

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. The tool submits nothing but the wrapper job's one `flux batch`.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (2 nodes + Rabbit, 40 min for the Elmerfold smoke).
- Never start moirai-service as a flux job in this app: after the drain, prestage, the dbcast steps and the app job each need the whole worker node (`--exclusive`, `--nodes $num_nodes --tasks-per-node 1`), so a one-core service job blocks them until the limit. Never `dftracer_service start` under `flux run` either: it never returns.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each job has its own ring.
- Do not edit run.sh or the wrapper by hand: regenerate.

## Corner cases
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: the app did not reach the ring. `moirai_validate` names the hosts that only have spill traces.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit` (the wrapper job has limit+300 s; the batch has `-t`).
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` shows a default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (gone). The binary also holds `../../dftracer/bin/dftracer_service`, relative to its own real path. `moirai_service_resolve` resolves it; the wrapper exports `MOIRAI_DFTRACER_SERVICE` when the default is missing and exits 2 with a clear line when nothing exists. Pass `service_venv`.
- Wrapper exit codes: 2 setup problem (original script, env file, moirai-service or dftracer_service missing), 3 ring not visible, 130/143 INT/TERM, otherwise the app's rc.

## Unconfirmed (state it to the user; nothing below was run)
- Whether `moirai-service start` returns by itself (daemonizes) or stays in the foreground: neither `--help` nor a start was run. The wrapper does not depend on it (background plus ring wait; stop then reaps the pid, kill after 10 s).
- libstdc++ shadowing: `submit_smsp.sh` puts `${ELMER_ENV}/lib` first in `LD_LIBRARY_PATH` (line 110) AFTER the wrapper started the service, so the service is not affected, but the app process loading moirai/dftracer libs may pick the conda libstdc++. Symptom: `GLIBCXX_... not found` in the chunk log. Not tested.
- Trace file names per host: assumed to carry the host name; if not, `host_traces` is inconclusive.
- Whether the GPU inference server runs on the same node as the driver (the one running the wrapper): a server on another node is not traced by this service.
- `flux bulksubmit` environment: assumed to pass run.sh's `ELMER_*` to the wrapper (the original needs them, so the app job already relies on it).
- `elmerfold_run_script --submit-script` must exist when run.sh is generated and the wrapper must be executable: the tool writes it with mode 755 before submitting; whether the generator checks the path at generation time is not known.
- `flux jobs -o {returncode}`/`{result}` and `flux batch -t <min>` are taken from memory, not run here.

## Corrector
Predictor `moirai_smoke_run`, `moirai_baseline_run`, `moirai_batch_plan`, `moirai_batch_render`, `moirai_service_resolve`, validator `moirai_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval; batch: the wrapper line `batch job ... FAILED`, then the batch `--output` file and the chunk log in `test_logs` (wrapper messages start `moirai-wrapper:`) |
| `trace`, `service_trace` | read `service-<host>-<job>.log` next to the traces; `/dev/shm`, ring size |
| `host_traces` | a host listed in `hosts.txt` has no trace: its service did not start or file names lack the host; read `service-<host>-<job>.log` |
| `host_service_trace` | only spill traces: the app did not see the ring; same `MOIRAI_SERVICE_SHM` in the app env (the wrapper exports it before the child), `/dev/shm` on that host |
| `activities`, `types_seen` | the annotated path did not run: back to the spec or an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |
| plan: "does not reference the wrapper" | regenerate run.sh with `--submit-script <wrapper path>` printed by the plan; do not edit run.sh |
| plan: "annotated tree" | run.sh was generated from the original tree: regenerate from the annotated copy |
| plan: "no ':::' input list" / "expected exactly one flux bulksubmit" | run.sh changed shape: update `check_run_sh` (`capability_update`) after reading it |
| `moirai-wrapper: ... no dftracer_service` (exit 2) | `moirai_service_resolve` with the venv; install dftracer there or export `MOIRAI_DFTRACER_SERVICE` |
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
- Run settings that worked for this app: `lesson_add`. Settle each Unconfirmed item with the run that proves it (`(confirmed YYYY-MM-DD)`).
- A failure mode the validator missed: `capability_update` with a check for it.
