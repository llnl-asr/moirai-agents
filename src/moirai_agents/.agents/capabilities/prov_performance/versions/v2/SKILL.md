---
name: prov_performance
description: Performance aspect of provenance - how to improve efficiency: the bottleneck, time per function and layer, I/O, parallelism, app- vs system-centric. Load when a question or a check is about time, speed, bottlenecks, I/O volume or bandwidth, parallelism or concurrency, or what the nodes did, and when annotating so the run records it (perf_annotate plans the dftracer performance annotation of hot code).
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] [[dftracer-optimizer]] [[dftracer-annotate-python]] [[dftracer-annotate-general]] [[dftracer-cheatsheet]]

# Performance

Scope: How to improve efficiency: the bottleneck (where time goes, by activity, function, I/O call, process, host), I/O volume, time and bandwidth per file and operation, parallelism (processes, hosts, how much of the wall time ran concurrently), and app-centric vs system-centric time (the app's own work vs the I/O and system calls under it, and the node's CPU, disk and network counters).

## Rules
- Answer a user's question with `evidence_answer` and these operations (each step is a TraceViewer query through prov_read; the answer is kept with the trace):
  - `perf_time` {by: activity|function|io|process|host|layer, top?}: where the time went (layer: app-centric activities and functions vs system-centric I/O calls)
  - `perf_io` {by: file|call|host, top?}: bytes read and written, time, bandwidth (calls on no file, such as waitpid, are reported apart)
  - `perf_parallelism` {}: processes, hosts, wall time (first start to last end), busy time, average concurrency
  - `perf_system` {metric: cpu|disk|network}: the nodes' psutil counters per host (system-centric)
- `performance_summary` gives the standard picture of a run; `performance_check` says what the annotation did not capture, with the fix.
- What the annotation must record for this aspect: dftracer I/O tracing on (MOIRAI_TRACE_IO=1: POSIX/STDIO calls with bytes and time), its metadata on (FH/HH/SH/start/end records), psutil system counters, the activities' own durations, and dftracer function annotation of the app's hot code (regions the provenance activities do not cover).
- The bottleneck is the largest share of time at the level asked; name it with its share and say whether it is app-centric (activities, functions) or system-centric (I/O, waits).
- Performance data comes from dftracer's events in the same traces (POSIX/STDIO calls, function/region events, sys/io/net counters) and the activities' durations: never put timings in entities.
- The app's own hot code is performance-annotated by prov_annotate's `performance` spec section: dftracer's own Python API calls (dft_fn decorator / region, ai.compute|data|comm|checkpoint|io categories) inserted directly into the copy, in the same pass as the provenance annotation. `perf_annotate` PLANS the entries (file, function or statement range, category, optional name) from perf_time by function and by activity (the time not covered by provenance activities), the source (functions found, python_estimate_function_cost) and the spec; the user reviews them at the annotate gate. Reuse dftracer-agents' rules (Rule 0 what to skip, comp=io|comm|mem|cpu, hot-loop exclusion): a tight function is not decorated per iteration, its caller loop or region is.
- Evidence: Elmerfold smoke traces: 18 provenance activities = 30.994 s, 0.000 s in dftracer-annotated functions, run_inference 76.5% of it; subprocess waits and run_inference internals are the first entries.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns performance only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Optimization advice goes through dftracer-agents' diagnose and optimizer agents (dfanalyzer presets, the optimization knowledge base); this capability measures, it does not tune.
- perf_annotate only plans; the copy is written by prov_annotate's annotate_apply, never by hand. Python first; C/C++ is a follow-up.

## Corner cases
- waitpid dominates I/O time when the app waits on subprocesses: that is waiting, not I/O; perf_io keeps it apart. A subprocess wait is annotated as a region with comp=io at the call site, not by decorating the callee.
- Small calls are aggregated by dftracer (cat posix, args.dft_cnt): they are folded into the counts and time.
- Average concurrency = activity busy time / wall time; it says how parallel the work was, not how many cores were used (perf_system cpu does).
- A function already carrying a provenance activity is not decorated twice: its internals are annotated as regions.

## Corrector
Validator `performance_check` (and `performance_selftest` for the capability itself).

| Failed check | Correct |
|---|---|
| `activities_timed` | annotate the functions as activities |
| `io_traced` | run with dftracer I/O tracing (MOIRAI_TRACE_IO=1, moirai_integrate) |
| `system_counters` (advice) | keep dftracer's system counters on |
| `app_functions_annotated` (advice) | run perf_annotate, review its entries, send them as the spec's `performance` section, annotate_apply, annotate_validate, re-run |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.performance with its selftest.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of performance question that needed a hand-built plan becomes an operation in core.aspects.performance with a selftest case.
- A performance entry that cost more than it showed (per-iteration overhead) or missed time becomes a lesson with the trace evidence.
