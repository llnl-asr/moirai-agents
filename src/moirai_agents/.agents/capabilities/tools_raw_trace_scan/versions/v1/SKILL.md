---
name: tools_raw_trace_scan
description: Scan the raw *.pfw.gz provenance traces of a (live) pipeline in parallel for attribute values without the TraceViewer index. Load when query_summary fails with a RocksDB LOCK or the index takes 5+ minutes, or a question needs a mean or count over a very large live run.
---

Cross-references: [[prov_query]] [[workload_elmerfold]]

# Raw trace scan

## Rules
- Use `raw_attribute_scan(trace_dir, attribute, entity_type)` instead of waiting on a locked index; never sleep-poll the LOCK.
- trace_dir is the pipeline's `traces/live_run/raw` (the *.pfw.gz files). The ad-hoc 16-process version took about 40 s for 31 files / 3 GB; this tool uses threads, so time it on first use and use a detached run if it exceeds the tool timeout.
- Quote which mean: over records (every record) or over distinct ids (identical values share one attribute id). Say both when they differ.
- The result is not an evidence_answer: tell the user the Query view shows no plan or cited records for it.
- Run `raw_scan_selftest` after any change.

## Constraints
- Read-only; never write into the trace dir. Numbers come only from the tool output.

## Corner cases
- Elmerfold live 32 node run (elmerfold-live-32n-20261109-140945): best_structure mean_plddt 79.00 over 99,801 records (78.93 over 98,876 distinct ids); structure_metrics 77.58 over 199,579 records; min 21.29, max 98.31. The live run DOES record mean_plddt (earlier lessons saying it predates quality attributes are wrong).
- The count of best_structure records equals the number of result tars; sequences the app itself failed have none.
- A pattern miss returns no types: check the attribute name against the entity description.
- Capability modules are loaded dynamically: a ProcessPoolExecutor fails with PicklingError, use threads or a detached script.

## Corrector
Validator `raw_scan_selftest`.

| Failed check | Correct |
|---|---|
| selftest error | the record format changed: read one `zcat | grep mean_plddt=` line and fix the regex with capability_update |
| no *.pfw.gz files | wrong dir: pass the pipeline's raw trace dir |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new attribute question answered this way: lesson_add here with the value and pipeline.
