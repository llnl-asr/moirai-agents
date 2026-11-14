---
name: prov_read
description: Reading provenance traces - the one reader (core.provtrace on dftracer-utils' TraceViewer) that every graph, query, check and aspect capability uses, so traces are read the same way everywhere. Load when anything needs to read a trace, or a read is slow or wrong.
---

Cross-references: [[prov_query]] [[prov_traceability]] [[prov_accountability]] [[prov_decision]] [[prov_performance]]

# Reading traces

## Rules
- Every read of a trace goes through core.provtrace: `T.read` (the provenance graph), `T.row_counts` (counts by group-by), `T.ensure_index` (the TraceViewer index, all cores), `T.tv()` (TraceViewer itself, for the evidence engine's Viewer and the aspects).
- Graph views (aggregate), queries (core.evidence and the four aspects), validators and published-trace checks all read through it.
- An index lives beside the traces (`<dir>/.dftindex`) when the folder is writable, else in a per-user cache keyed by the folder; a locked shared index is never a reason to parse files: build a private index.
- A standalone script that runs outside moirai-agents (a published package's prov_check.py, an ad-hoc query in the moirai Python) uses dftracer.utils.TraceViewer directly, never gzip or JSON parsing.

## Constraints
- No capability imports dftracer.utils or opens, decompresses or parses a trace file: the capability store refuses it (direct_trace_reads, dftracer_utils_import).
- Do not rebuild the whole graph to compare counts: use row_counts, or check that the files are the counted ones (size + sha256).

## Corner cases
- A trace still being written: re-index the changed files (ensure_index force) before reading.
- dftracer columns differ per file (a small file's index may not list every args column): select only columns the index has, else read whole rows.
- Group-by output names: count, sum_<col>, mean_<col>, min_<col>, max_<col>; a computed column (ts + dur) goes through with_column before select, not after a group_by.

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Validator `prov_read_selftest`.

| Failed check | Correct |
|---|---|
| selftest counts differ | dftracer-utils changed how it reads a record: compare T.read on the selftest trace with the written records and fix core.provtrace |

## Self-heal
- An index error (LOCK, stale): build a private index (state_dir cache) and say so; never fall back to reading files by hand.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new reading need becomes a function in core.provtrace with a selftest case, not a reader elsewhere.
