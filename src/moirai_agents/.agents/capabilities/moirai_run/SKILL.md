---
name: moirai_run
description: Run the annotated application under moirai-service, a smoke run then the baseline, with live provenance on the Dashboard. Load for the moirai_smoke and baseline stages.
---

# Run with moirai

## Rules
- `moirai_smoke_run` first, same small test as the smoke stage. If the original passed and this fails, the annotation is the suspect.
- Before the baseline, the gate must settle what runs, where, at what scale, for how long. Use `ask_user`, then `moirai_baseline_run` with exactly that.
- While it runs, follow it with `moirai_run_status` and tell the user the Dashboard is live. Then `moirai_validate`.

## Constraints
- Traced runs of any size go through a launcher on compute nodes, never on a login node.
- Ask before more than one node or an hour. `limit` is a hard stop: set it from what the user approved.
- `moirai-service` starts before the app and stops after it, with the same `MOIRAI_LOG_FILE`; the generated script guarantees this. Do not start it by hand.
- Do not change `MOIRAI_SERVICE_SHM` by hand: each run has its own ring.

## Corner cases
- Several nodes: one service per node, one trace per host. Each needs `/dev/shm` on that node.
- Tiny `/dev/shm`: lower `MOIRAI_PC_BLOCKS` in the environment; application and service must agree.
- Only `*-spill.pfw.gz` files: no service was reachable. Nothing is lost, but fix it.
- Activities are never aggregated, so an activity in a tight loop grows the trace fast: watch the Dashboard's rate.
- Exit code 124 or 137: it hit `limit`.
- `libdftracer_core... cannot open shared object` in the log: `moirai_env.sh` predates the fix or points at another venv; run `integrate_apply` again.

## Corrector
Predictor `moirai_smoke_run`, `moirai_baseline_run`, validator `moirai_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `exit_0` | compare with the smoke stage; a moirai line points back to `prov_annotate`; rc 124 or 137: raise `limit` with the user's approval |
| `trace`, `service_trace` | read the service log next to the trace prefix; `/dev/shm`, ring size |
| `activities`, `types_seen` | the annotated path did not run: go back to the spec or use an input that reaches it |
| `no_dangling` | an entity is related but never declared: correct the spec |

## Self-heal
- `exit_0` failed: compare `log_tail` with the smoke stage; failures with `moirai` in them are annotation or link problems.
- `activities` is zero: the app ran from somewhere other than the annotated tree, or the annotated functions are not reached.
- Service does not start: read the service log next to the trace prefix (`No space left on device` means the ring).

## Self-learn
- Run settings that worked for this app: `lesson_add`.
- A failure mode the validator missed: `capability_update` with a check for it.
