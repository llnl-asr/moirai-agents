---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard; single command or batch mode for apps launched as `flux batch run.sh` (elmerfold, drained launch node, Rabbit). Load for the moirai_smoke and baseline stages; search words moirai-service, flux batch, drain, spill, dftracer_service never returns.
---

Cross-references: [[workload_elmerfold]] [[tools_moirai]] [[flux-alloc]] [[dftracer-trace-utils]]

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.
- Apps launched as `flux batch run.sh` (a generated script with `#flux:` headers): use batch mode. Call `moirai_batch_plan` (dry run), show the user the generated script, the wrapper and the exact `flux batch -t <min>m --queue <q> .../run-moirai.sh` command, get approval, then `moirai_smoke_run(batch_script=..., queue=..., limit=...)`. Never submit without that review.
- Batch mode inserts only two blocks into the original run.sh: the preamble after the last `#flux:` line (env, MOIRAI_LOG_FILE=<traces>/raw/prov, per-run MOIRAI_SERVICE_SHM including FLUX_JOB_ID, dftracer_service resolution) and the service block right after `flux resource drain "${crit_ranks}"`. The rest of run.sh is untouched.
- `limit` seconds become `flux batch -t <ceil(limit/60)>m`; the wrapper job gets `limit + queue_wait` so queue time does not eat the run.

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node. Batch mode submits from the wrapper (a login-node shell that only runs `flux batch` and `flux watch`).
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved (smoke approved: 2 nodes + Rabbit, 40 min = limit 2400).
- `moirai-service` starts before the app and stops after it, with the same `MOIRAI_LOG_FILE`; the generated script guarantees this. Do not start it by hand.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each run has its own ring.
- `dbcast` must be on PATH of the SUBMITTING shell (flux batch propagates the environment): the wrapper loads `mpifileutils/0.11.1` and fails clearly if `dbcast` is missing.
- Never edit the app's run.sh or the workload capability from here: only run-moirai.sh (a copy) is generated.

## Corner cases
- Several nodes: one service per node, one trace per host. Each needs `/dev/shm` on that node.
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: no service was reachable. Nothing is lost, but fix it. `moirai_trace_hosts` says which hosts.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit`.
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.
- `moirai-service --help` prints a build-time default `dftracer_service` under `/tmp/<user>/pip-build-env-...` (gone). The binary also holds `../../dftracer/bin/dftracer_service`; the preamble keeps `MOIRAI_DFTRACER_SERVICE` if it exists, else the help default if it exists, else the first existing of: `dftracer_service=` argument, `<moirai-service dir>/../lib/python*/site-packages/dftracer/bin/dftracer_service`, `dftracer_service` on PATH. Which one the binary really uses is UNCONFIRMED: read the `moirai: moirai-service=... dftracer_service=...` line in the log.
- Compute nodes cannot create files under `python3.N/site-packages` (reading is fine): nothing in the preamble writes there.
- run.sh's own `#flux: --cwd` may point at the unannotated source: the plan warns when the annotated tree is never mentioned.

## Memory traps (confirmed earlier, applied by the generated script)
1. `dftracer_service start` under `flux run` never returns (the daemon stays a child of the task). The launch node starts it with `setsid nohup ... &` in the script; the other nodes with `flux submit` (detached), and readiness is waited on in `/dev/shm`, never on the command's return.
2. In an allocation larger than the job, start/app/stop can land on DIFFERENT nodes. Every start, ring probe and stop is pinned with `--requires=host:<h>` from one host list computed once (`_hosts_sched`, `_hosts_crit`), and the trap stops those same hosts. The list is printed as `MOIRAI_HOSTS launch=... sched=...`.

## Unconfirmed (first run proves or breaks these)
- Where each process lands: the launch (drained) node runs the script and the plain service; the driver, process pool and GPU inference server are expected on the other node(s). The one service per node covers any annotated process on that node only if they all see the same `/dev/shm` and MOIRAI_SERVICE_SHM.
- The service job on a schedulable node takes one core. The app's `flux bulksubmit --nodes 1 --exclusive` jobs may then wait for a node that is never free. If `flux jobs` shows the app jobs pending with the service job running, that is the cause: change the service to a mechanism the exclusive job does not conflict with (ask first), do not remove `--exclusive`.
- `flux submit --requires=host:<name>`, `flux job status -v` exit code, and the file-name host token used by `moirai_trace_hosts` are untested on this machine.
- Spindle (`-o spindle`) is used by the app's jobs, not by the service job.

## Corrector
Predictor `moirai_smoke_run`, `moirai_baseline_run`, `moirai_batch_plan`, validator `moirai_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval; `wrapper:` lines in the log: module/dbcast/`flux batch` problem in the wrapper; `moirai:` lines: preamble or service start |
| `trace`, `service_trace` | read `<traces>/../service/start-<host>.out/.err` and the service log; `/dev/shm`, ring size; `MOIRAI_HOSTS` vs hosts of the trace files; spill-only on one host = that host's service never came up or the app ran on a host that was not listed |
| `hosts_traced` | a listed host wrote no trace: no annotated process ran there (the placement guess was wrong) or its service wrote elsewhere |
| `activities`, `types_seen` | the annotated path did not run: go back to the spec or use an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |
| `moirai_batch_plan` error | run.sh changed shape: read the message (header, drain line, num_nodes line, existing trap), read run.sh, then `capability_update` the pattern. Never guess an insertion point |
| `no usable dftracer_service` in log | pass `dftracer_service=` with the real file (check `<venv>/lib/python3.N/site-packages/dftracer/bin`) |

## Self-heal
- `exit_0` failed: compare `log_tail` with the smoke stage; failures with `moirai` in them are annotation or link problems.
- `activities` is zero: the app ran from somewhere other than the annotated tree, or the annotated functions are not reached.
- Service does not start: read the service log next to the trace prefix (`No space left on device` means the ring).
- Batch job stuck pending or stopped: `moirai_run_stop` cancels the batch job; its EXIT trap stops the services on the listed hosts. If the batch job is already gone and a ring remains, list `/dev/shm/moirai-agents-*` on the hosts through the user's launcher and ask before removing.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Run settings that worked for this app: `lesson_add`; the confirmed placement of each process and the real dftracer_service path go to the skill as `(confirmed YYYY-MM-DD)` after the first batch run.
- A failure mode the validator missed: `capability_update` with a check for it.
