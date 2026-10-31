---
name: prov_dashboard
description: Open the live provenance Dashboard tab on a running or finished trace. Load for the dashboard stage, or when a user wants to see a run's events.
---

# Dashboard

The Dashboard tab is dfdashboard, run by the web UI on a private port and reached through the web UI's own port. Its Provenance view reads the trace directly and pushes new events to the page while the run grows: time runs top to bottom, with details on the right.

## Rules
- A moirai run opens it by itself. Use `dashboard_open` for existing traces, with `live=false`.
- Point the user at the tab, then say what to look at: which activities, which output entities.
- At the gate, ask what they want to find out: that drives the query stage.

## Constraints
- Reads traces only; it never changes them.
- Do not start dfdashboard yourself: the web UI owns it and stops it with everything else.

## Corner cases
- Live view lags by the service's flush interval, 250 ms by default in our runs, or longer when the run is quiet.
- A member still being written is skipped and read on the next poll; counts only ever grow.
- Very large runs: the graph view draws the most recent items; the counters and queries cover everything.
- Spill files are read too, so a run with no service still shows up.

## Corrector
Predictor `dashboard_open`, validator `dashboard_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `open` | run `dashboard_open` |
| `graph` | wrong path, or traces without moirai records: ask the user for the right ones |

## Self-heal
- Nothing appears: `moirai_run_status` counts tell whether the trace is growing. If it is, the page reconnects on reload; if not, look at the run.
- `graph` failed: wrong directory, or traces from before moirai with no EH records.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A view users keep asking for: `pipeline_feedback(kind=process)`.
