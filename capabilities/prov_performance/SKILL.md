---
name: prov_performance
description: Performance aspect of provenance - how to improve efficiency: the bottleneck, time per function and layer, I/O, parallelism, app- vs system-centric. Load when a question or a check is about time, speed, bottlenecks, I/O volume or bandwidth, parallelism or concurrency, or what the nodes did, and when annotating so the run records it.
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] [[dftracer-optimizer]]

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
- The app's own hot code is performance-annotated with dftracer-agents' annotators (mcp__dftracer__python_annotate_file / clang_annotate_file, cost-gated, comp=io|comm|mem|cpu) on the functions the provenance activities do not cover; then perf_time by function shows them.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns performance only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Optimization advice goes through dftracer-agents' diagnose and optimizer agents (dfanalyzer presets, the optimization knowledge base); this capability measures, it does not tune.

## Corner cases
- waitpid dominates I/O time when the app waits on subprocesses: that is waiting, not I/O; perf_io keeps it apart.
- Small calls are aggregated by dftracer (cat posix, args.dft_cnt): they are folded into the counts and time.
- Average concurrency = activity busy time / wall time; it says how parallel the work was, not how many cores were used (perf_system cpu does).

## Corrector
Validator `performance_check` (and `performance_selftest` for the capability itself).

| Failed check | Correct |
|---|---|
| `activities_timed` | annotate the functions as activities |
| `io_traced` | run with dftracer I/O tracing (MOIRAI_TRACE_IO=1, moirai_integrate) |
| `system_counters` (advice) | keep dftracer's system counters on |
| `app_functions_annotated` (advice) | performance-annotate the app's hot code (dftracer-agents annotators, cost-gated) |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.performance with its selftest.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of performance question that needed a hand-built plan becomes an operation in core.aspects.performance with a selftest case.
