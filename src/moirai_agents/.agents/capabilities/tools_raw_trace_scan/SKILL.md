---
name: tools_raw_trace_scan
description: Statistics of one recorded attribute (mean, median, min, max, n) over every entity of a pipeline's traces, read through TraceViewer, with a private index when the shared one is locked. Load when query_summary or evidence_answer fails with a RocksDB LOCK, or a question needs a mean or count over a very large live run.
---

Cross-references: [[prov_query]] [[workload_elmerfold]]

# Attribute statistics over the traces

## Rules
- Traces are read ONLY through TraceViewer (this tool uses the evidence engine's stats query). Never open, decompress, zcat or grep a trace file yourself, and never write a script that does.
- Prefer `evidence_answer` with a `stats` step: its answer is cited in the Query view. Use `raw_attribute_scan(trace_dir, attribute, entity_type)` when that is blocked by a locked index; never sleep-poll the LOCK.
- trace_dir is the pipeline's raw trace dir (`traces/<stage>/raw`). A locked shared index makes the tool build a private one under the project's state dir: slower the first time on a large run, then fast.
- Quote which mean: `mean` counts each value once per entity it describes, `mean_over_distinct` once per distinct value. Say both when they differ.
- The result is not an evidence_answer: tell the user the Query view shows no plan or cited records for it.
- Run `raw_scan_selftest` after any change.

## Constraints
- Read-only; never write into the trace dir. Numbers come only from the tool output.

## Corner cases
- Elmerfold live 32 node run (elmerfold-live-32n-20261109-140945): best_structure mean_plddt 79.00 over 99,801 records (78.93 over 98,876 distinct ids); structure_metrics 77.58 over 199,579 records; min 21.29, max 98.31 (measured by the earlier file-parsing version: re-measure with this tool before quoting). The live run DOES record mean_plddt.
- The count of best_structure records equals the number of result tars; sequences the app itself failed have none.
- No types returned: check the attribute name against the entity description (prov_query).

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Validator `raw_scan_selftest`.

| Failed check | Correct |
|---|---|
| selftest error | the evidence stats query changed: run evidence_answer with the same stats step on the selftest's trace and fix this tool with capability_update |
| not a directory / no trace files | wrong dir: pass the pipeline's raw trace dir |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new attribute question answered this way: lesson_add here with the value and pipeline.
