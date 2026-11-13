---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; also apps launched by a generated `flux batch` run.sh (Elmerfold) with one service per schedulable node, after a dry-run. Load for the moirai_smoke and baseline stages, for 'flux batch', 'run.sh', 'drain', 'dftracer_service not found', 'spill-only', 'No running server found'.
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
- What is inserted: after the header, a preamble (source `moirai_env.sh`, `MOIRAI_LOG_FILE=<traces>/raw/prov`, `MOIRAI_DATA_DIR`, a per-run `MOIRAI_SERVICE_SHM`, `MOIRAI_PC_*`, the `dftracer_service` fix, EXIT/TERM/INT trap); after the drain line, the services: a plain `moirai-service start` on the launch node and, per schedulable node, a pinned detached `flux submit -N1 -n1 -c1 --requires=host:<h> --setattr=exclusive=false`, then a wait for the ring on every node (failing the run clearly if one is missing). The trap stops the same hosts it started (one list, `hosts.txt` beside the traces).

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. Batch mode submits nothing from the tool except the wrapper's one `flux batch`.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (2 nodes + Rabbit, 40 min for the Elmerfold smoke).
- `moirai-service` starts before the app and stops after it, with the same `MOIRAI_LOG_FILE`; the generated script guarantees this. Do not start it by hand.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each run has its own ring.
- `dftracer_service start` under `flux run` never returns: start detached, then wait for the ring. Start, app and stop must use the same pinned hosts.

## Corner cases
- Several nodes: one service per node, one trace per host. Each needs `/dev/shm` on that node.
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: no service was reachable. Nothing is lost, but fix it. In batch mode `moirai_validate` names the hosts that only have spill traces.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit` (the wrapper has limit+300 s; the batch itself has `-t`).
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` shows a default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (build time, gone). The binary also holds `../../dftracer/bin/dftracer_service`, relative to its own real path, e.g. `<venv>/lib/python3.13/site-packages/dftracer/bin`. `moirai_service_resolve` resolves it; the preamble exports `MOIRAI_DFTRACER_SERVICE` to the existing file when the default is missing, and fails with a clear line when nothing exists. Pass `service_venv` for the fallback search.
- Launch node is drained (`flux resource drain`): `flux run`/`submit` cannot place work on it, hence the plain call there and the pinned submits only AFTER the drain section.

## Unconfirmed (state it to the user; nothing below was run)
- Where each process runs: the script runs on the launch node (assumed from the user, not observed); the driver, process pool and GPU inference server on the other node(s). The service submits are pinned to those hosts, but whether the app's own processes land on the same hosts is not confirmed. Check `hosts.txt`, `service-<host>.log` and `per_host_traces`.
- EXCLUSIVE CONFLICT (likely): the original run.sh asks for `--exclusive` nodes (prestage, dbcast, `flux bulksubmit --exclusive`) AFTER the drain. A running one-core moirai-service job on a schedulable node probably makes that node not free for an exclusive request, so those steps could pend forever. The dry run lists these lines as a warning. Untested alternatives: start the service from inside the app job's own wrapper, or `flux exec -r <rank>` (not a scheduled job). Do not submit before the user has seen this.
- `flux hostlist -e "${crit_nodes}"`, the `--setattr=exclusive=false` form, `flux jobs -o {returncode}` and `{result}`, `flux batch -t <minutes>` and the plain `moirai-service start` returning at once are taken from the facts given and from memory, not run here.
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
| plan warning: exclusive | ask the user; see Unconfirmed |
| `moirai: no dftracer_service found` | `moirai_service_resolve` with the venv; install dftracer into the venv or export `MOIRAI_DFTRACER_SERVICE` |
| `moirai: ring ... not visible on <h>` | read `service-<h>.log`; ring size; was the submit placed on `<h>`? |

## Self-heal
- `exit_0` failed: compare `log_tail` with the smoke stage; failures with `moirai` in them are annotation or link problems.
- `activities` is zero: the app ran from somewhere other than the annotated tree, or the annotated functions are not reached.
- Service does not start: read the service log next to the trace prefix (`No space left on device` means the ring).
- `No running server found` on stop: start and stop went to different hosts; stop each host by hand with `flux run -N1 -n1 -c1 --requires=host:<h> moirai-service stop`.
- A run that hangs at `Prestage` or `dbcast` after the service started: the exclusive conflict; `moirai_run_stop`, tell the user.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules. Elmerfold's run.sh shape, once run, belongs in `workload_elmerfold`.
- Run settings that worked for this app: `lesson_add`. Settle each Unconfirmed item with the run that proves it (`(confirmed YYYY-MM-DD)`).
- A failure mode the validator missed: `capability_update` with a check for it.
