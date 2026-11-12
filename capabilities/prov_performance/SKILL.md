---
name: prov_performance
description: Performance aspect of provenance - how to improve efficiency: the bottleneck, time per function and layer, I/O, parallelism, app- vs system-centric. Load when a question or a check is about time, speed, bottlenecks, I/O volume or bandwidth, parallelism or concurrency, or what the nodes did, and when annotating so the run records it (perf_annotate plans the dftracer performance annotation of hot Python and C/C++ code from the measured time and dftracer-agents' function cost model; perf_apply inserts it).
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] [[dftracer-optimizer]] [[dftracer-annotate-python]] [[dftracer-annotate-general]] [[dftracer-annotate-cpp]] [[dftracer-annotate-c]] [[dftracer-cheatsheet]]

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
- Performance annotation (the app's hot code): the `performance` section of prov_annotate's spec, written into the provenance copy by `perf_apply` in the same pass as the provenance (annotate_apply calls it), with dftracer's own API: Python `dftracer.python` (`ai.compute|data|comm|checkpoint|io` regions, `dft_fn` for other categories), C++ `DFTRACER_CPP_FUNCTION()` (RAII). `perf_annotate` PLANS the entries and the user reviews them at the annotate gate.
- The SELECTION MODEL is dftracer-agents' function cost model, reused and never copied: Python `python_cost.recommend` (python_estimate_function_cost / python_estimate_file_costs: AI/ML-aware scores, Rule 0 skips, lifecycle names), C/C++ `source_parser.extract_functions` (clang AST, `cost_info`: io x30, mpi x25, vendor x30, mem x15, loop x10, branch x3, call x2, body bonus) with `clang_estimate_function_cost`'s rules (lifecycle R6, vendor R7, score >= 20). `perf_annotate` combines the model with the measured time (perf_time by activity and by function): each entry lists score, recommendation, measured_s, measured_share_pct and reason, ranked by the share of the activity that hides it.
- Entry kinds: a function (`{file, function, category}`: decorator / RAII guard) or a statement range (`{file, in, anchor, count?, occurrence?, category, name?}`: a `with` region around a subprocess wait, an I/O call, a checkpoint, a communication, a loop of model calls). A loop is one region, never its body (hot-loop exclusion). Python category: compute|data|comm|checkpoint|io|device|dataloader|pipeline|other or any word (`dft_fn`'s category IS the classification); C++ `comp`: io|comm|mem|cpu.
- Evidence: Elmerfold smoke traces: 18 provenance activities = 30.994 s, 0.000 s in dftracer-annotated functions, run_inference 76.5% of it; subprocess waits and run_inference internals are the first entries. The baseline (f3jbLVAD3VuR, 220 activities) is the trace to plan from.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns performance only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Optimization advice goes through dftracer-agents' diagnose and optimizer agents (dfanalyzer presets, the optimization knowledge base); this capability measures, it does not tune.
- perf_annotate only plans; the copy is written by perf_apply (through annotate_apply), never by hand. The user's source is read-only.
- Python and C++ are applied; C is planned only (it needs FUNCTION_START and FUNCTION_END before every exit point: apply it with dftracer-agents' clang_annotate_file, follow-up); headers are never annotated (dftracer-annotate-cpp).

## Corner cases
- waitpid dominates I/O time when the app waits on subprocesses: that is waiting, not I/O; perf_io keeps it apart. A subprocess wait is annotated as a region (category subprocess) at the call site, not by decorating the callee.
- Small calls are aggregated by dftracer (cat posix, args.dft_cnt): they are folded into the counts and time.
- Average concurrency = activity busy time / wall time; it says how parallel the work was, not how many cores were used (perf_system cpu does).
- A function already carrying a provenance activity is not decorated twice: its internals are annotated as regions.
- Async functions and generators are never decorated (a decorator times creation, not the run); a function called per iteration of a loop with no I/O, communication or loop of its own is skipped by the hot-loop exclusion; both yield only to `force` for the cost gate, never for async/generator.
- A region is wrapped in `with`, so its statements are re-indented: a range with a multi-line string, a statement sharing its line, or an `elif` anchor is refused with the reason.
- The dftracer Python API needs `initialize_log` per process: the inserted block does it once (guarded by the instance's logger) and registers finalize. Whether pydftracer's events land in the SAME trace as moirai's (moirai brings its own native dftracer) is proven only by a traced run: after the re-run, `perf_time by function` must list the entries and `performance_check` app_functions_annotated must pass; if it does not, record it as a lesson with the trace.
- DFTRACER_CPP_INIT is not inserted here: C++ events need the dftracer core initialized by moirai's own initialize; verify with the same re-run.
- clang is needed for C/C++ planning and applying; without it perf_annotate lists the file as skipped with the error; pass `compile_flags` (-I, -D, -std=) for real C++ projects, as dftracer-agents' extract_functions requires.

## Corrector
Validator `performance_check` (and `performance_selftest`, `perf_annotate_selftest` for the capability itself, `perf_validate` for the inserted calls).

| Failed check | Correct |
|---|---|
| `activities_timed` | annotate the functions as activities |
| `io_traced` | run with dftracer I/O tracing (MOIRAI_TRACE_IO=1, moirai_integrate) |
| `system_counters` (advice) | keep dftracer's system counters on |
| `app_functions_annotated` (advice) | run perf_annotate, review its entries, send them as the spec's `performance` section, annotate_apply (perf_apply runs in it), perf_validate, re-run |
| `perf_validate`: `parses`, `dftracer_imported_once`, `dftracer_included_once`, `names_resolve`, `regions_balanced`, `inserted` | the copy was edited or the entry is wrong: fix the entry, annotate_apply again (it strips and re-inserts) |
| `perf_validate`: `entries_accounted` | an entry names a missing file or function: read perf_apply's `problems` and fix the entry |
| a region or function entry refused | read its `reason` (cost gate, hot-loop, async, generator, multi-line string): annotate the caller loop, narrow the range, or `force` it with the reason |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.performance with its selftest.
- perf_annotate says no trace times: the pipeline has no traces yet; the plan from the source alone is still valid, re-plan after the baseline.
- C/C++ file skipped with a clang error: pass `compile_flags`, or check that clang is on PATH.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of performance question that needed a hand-built plan becomes an operation in core.aspects.performance with a selftest case.
- A performance entry that cost more than it showed (per-iteration overhead) or missed time becomes a lesson with the trace evidence; a statement pattern the planner missed (a wait, a model call) becomes a call vocabulary entry with a selftest case.
