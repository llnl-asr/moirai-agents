---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; also apps launched by a generated `flux batch` run.sh (Elmerfold) with moirai-service started on every rank by `flux exec -r all` (not a scheduled job), after a dry-run. Load for the moirai_smoke and baseline stages, for 'flux batch', 'run.sh', 'drain', 'flux exec', 'dftracer_service not found', 'spill-only', 'No running server found', 'bulksubmit waits forever'.
---

Cross-references: [[tools_moirai]] [[workload_elmerfold]] [[flux-alloc]] [[bug-dftracer-service-start-blocks-flux-run]] [[bug-dftracer-service-hosts-must-be-pinned]] [[app_smoke]]

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.
- Batch mode (an app launched by a generated `flux batch` run.sh, e.g. Elmerfold): ALWAYS `moirai_batch_plan` first (dry run: nothing written or started). Show the user the submit command, the warnings and the service section of the script. Only after their yes call `moirai_smoke_run` / `moirai_baseline_run` with `batch_script`, `queue` (default pdebug) and `limit` (seconds; the batch gets ceil(limit/60) minutes).
- Exact submit shape: a wrapper job (detached, `jobs.start`) loads `mpifileutils/0.11.1`, checks `dbcast`, runs `flux batch -t <minutes> --queue <queue> <traces>/run-moirai.sh`, `flux watch <id>`, reads `{returncode}` and `{result}` of the batch job and exits non-zero with a clear message unless COMPLETED and 0. `dbcast` must be on PATH of the SUBMITTING shell: flux batch propagates the environment.
- The rewrite is by pattern, never by guess: the `#flux:` header, `crit_ranks`, `crit_nodes`, the `num_nodes - num_crit_nodes` line and exactly one `flux resource drain` in that order. Otherwise it stops with "run.sh changed shape" and starts nothing: fix `moirai_batch_render`'s patterns for the new shape, do not hand-edit run-moirai.sh.
- What is inserted: after the header, a preamble (source `moirai_env.sh`, `MOIRAI_LOG_FILE=<traces>/prov`, `MOIRAI_DATA_DIR`, a per-run `MOIRAI_SERVICE_SHM`, `MOIRAI_PC_*`, the `dftracer_service` fix, EXIT/TERM/INT trap); after the drain line, ONE service mechanism: `flux exec -r all bash -c 'setsid nohup moirai-service start >> <traces>/service-$(hostname).log 2>&1 < /dev/null & echo started'`, then `flux exec -r all` running the `/dev/shm$MOIRAI_SERVICE_SHM` check (any rank without the ring fails the batch with exit 1 and a message naming the host), then `MOIRAI_HOSTS=<hosts>` and `MOIRAI_SERVICES_READY=1` lines. Hosts come from `flux exec -r all hostname` (also written to `hosts.txt` beside the traces). The EXIT/TERM trap runs `flux exec -r all moirai-service stop`: same ranks as the start, no host list, so start and stop cannot drift.
- `flux exec` is not a job: it bypasses the scheduler, creates no job, holds no cores, and reaches the drained launch node as well. That is why the app's own `flux bulksubmit --nodes 1 --exclusive` jobs keep their nodes. Never use `flux submit` / `--requires=host:` for the service.
- Verified on Tuolumne (confirmed 2026-10-30; pdebug, 1-node flux alloc; outputs `/p/lustre5/haridev/elmerfold/runs/flux-exec-test.out` and `/p/lustre5/haridev/elmerfold/runs/flux-exec-env.out`): `flux exec -r 0 bash -c 'setsid nohup sleep N >/dev/null 2>&1 & echo started'` returns after 1 s and the process stays alive; `flux resource list` afterwards still shows all 96 cores free and 0 allocated; `flux run -N1 --exclusive hostname` still runs; `flux exec` passes the CALLER's environment (a variable exported before `flux exec` was visible to the process) and the caller's cwd; `flux exec -r all hostname` reaches every rank; there is NO `flux idset` command on this system (do not use it).

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. Batch mode submits nothing from the tool except the wrapper's one `flux batch`.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (2 nodes + Rabbit, 40 min for the Elmerfold smoke).
- `moirai-service` starts before the app and stops after it, with the same `MOIRAI_LOG_FILE`; the generated script guarantees this. Do not start it by hand.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each run has its own ring.
- `dftracer_service start` under `flux run` never returns: start it detached (`setsid nohup ... &` inside `flux exec`), then wait for the ring. Start, app and stop must use the same ranks (`-r all` for both).
- The service must not be a scheduled job (`flux submit`/`flux run`) in batch mode: it would hold cores and starve exclusive jobs.

## Corner cases
- Several nodes: one service per rank, one trace per host. Each needs `/dev/shm` on that node.
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: no service was reachable. Nothing is lost, but fix it. In batch mode `moirai_validate` names the hosts that only have spill traces.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit` (the wrapper has limit+300 s; the batch itself has `-t`).
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` shows a default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (build time, gone). The binary also holds `../../dftracer/bin/dftracer_service`, relative to its own real path, e.g. `<venv>/lib/python3.13/site-packages/dftracer/bin`. `moirai_service_resolve` resolves it; the preamble exports `MOIRAI_DFTRACER_SERVICE` to the existing file when the default is missing, and fails with a clear line when nothing exists. Pass `service_venv` for the fallback search.
- Launch node is drained (`flux resource drain`): `flux run`/`submit` cannot place work there, but `flux exec` does not go through the scheduler, so it is unaffected; the service block still goes AFTER the drain line.
- `flux exec` hands the caller's environment to the process: the MOIRAI_* variables of the preamble reach the service and the ring check without being passed.

## Unconfirmed (state it to the user)
- Multi-rank behaviour of `flux exec -r all` (start, ring wait, stop on 2+ ranks, the nonzero exit when one rank fails) was only tested on 1 rank. The generated script as a whole has not run.
- That `moirai-service start` run under `setsid nohup` through `flux exec` creates the ring and writes per-host traces (the test used `sleep`); and that `moirai-service stop` via `flux exec -r all` finds each rank's pid file.
- Where each process runs: the driver, process pool and GPU inference server on the non-launch node(s) is assumed, not observed. Check `hosts.txt`, `service-<host>.log` and `per_host_traces`.
- `flux jobs -o {returncode}` and `{result}`, and `flux batch -t <minutes>`, are taken from the facts given and from memory, not run here.
- Trace file names are assumed to contain the host name; if not, `host_traces` is inconclusive.

## Corrector
Predictor `moirai_smoke_run`, `moirai_baseline_run`, `moirai_batch_plan`, `moirai_batch_render`, `moirai_service_resolve`, validator `moirai_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval; batch mode: the wrapper line `batch job ... FAILED` then the batch `--output` file |
| `trace`, `service_trace` | read the service log next to the trace prefix; `/dev/shm`, ring size; batch: `service-<host>.log` |
| `host_traces` | a host has no trace: its service did not start or the app never ran there; `hosts.txt`, `service-<host>.log`, placement (unconfirmed) |
| `host_service_trace` | a host has only spill traces: the app there did not see the ring; same `MOIRAI_SERVICE_SHM` in the app's env, `/dev/shm` on that host |
| `activities`, `types_seen` | the annotated path did not run: go back to the spec or use an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |
| plan: "changed shape" | update the patterns in `render_batch` (`capability_update`) after reading the new run.sh; never edit run-moirai.sh by hand |
| `moirai: no dftracer_service found` | `moirai_service_resolve` with the venv; install dftracer into the venv or export `MOIRAI_DFTRACER_SERVICE` |
| `moirai: ring ... not visible on <h>` | read `<traces>/service-<h>.log`; ring size; did `moirai-service start` die under `setsid nohup`? |

## Self-heal
- `exit_0` failed: compare `log_tail` with the smoke stage; failures with `moirai` in them are annotation or link problems.
- `activities` is zero: the app ran from somewhere other than the annotated tree, or the annotated functions are not reached.
- Service does not start: read `<traces>/service-<host>.log` (`No space left on device` means the ring).
- `No running server found` on stop: the service never started on that rank, or start and stop ran on different ranks; stop by hand inside the allocation with `flux exec -r <rank> moirai-service stop` (confirmed 2026-10-30: `flux exec` works inside the allocation, tests `/p/lustre5/haridev/elmerfold/runs/flux-exec-test.out`).
- A service that must be started by hand: `flux exec -r all bash -c 'setsid nohup moirai-service start >> <log> 2>&1 < /dev/null & echo started'`; never `flux submit`. Do not use `flux idset` (does not exist on Tuolumne, 2026-10-30); use `-r all` or an explicit rank.
- A run that hangs at `Prestage` or `dbcast`: check `flux resource list` (cores free/allocated) and `flux jobs -a`; with the `flux exec` service nothing of moirai should be allocated. If it is, someone started the service with `flux submit`: `moirai_run_stop`, tell the user.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules. Elmerfold's run.sh shape, once run, belongs in `workload_elmerfold`.
- Run settings that worked for this app: `lesson_add`. Settle each Unconfirmed item with the run that proves it (`(confirmed YYYY-MM-DD)`).
- A failure mode the validator missed: `capability_update` with a check for it.
