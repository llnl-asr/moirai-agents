---
name: prov_query
description: Answer questions about provenance - what came from what, what used what - with graph tools and the dftracer-utils DataFrame API. Load for the query stage of either pipeline.
---

# Query

## Rules
- `query_summary` first, then the user's questions in their terms.
- `query_entities` to find an id, `query_lineage` to walk it. `query_dataframe` for anything else: joins, group-bys, timelines.
- Every answer names the trace and the query that produced it. Show the numbers the tool returned, nothing else.

## Constraints
- Traces are read-only. The DataFrame index goes in the pipeline directory, never next to shared traces.
- `query_dataframe` runs on the machine the web UI runs on. For traces too large for a login node, ask before going further.
- Do not write raw gzip or JSON parsing in `query_dataframe`: use the API.

## Corner cases
- A `TraceViewer` reads events only by default: `graph` already uses phase `any` so metadata records are included.
- An entity's `EH` record appears once per process: use `.unique(subset="entity")` after selecting `args.id`.
- An activity has no id of its own: `(pid, ts)` names one.
- `args.relations` reads as numbered columns (`args.relations.used.0`, ...): unpivot them for edges.
- Roles and stores are numbers in the trace; `query_summary` and the graph tools already name them.

## Corrector
Predictor `query_summary`, `query_entities`, `query_lineage`, `query_dataframe`, validator `query_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `asked` | run the user's questions |
| `graph` | the traces are unreadable: check the path |
| a wrong answer | the question the user wanted, restated; a repeated query becomes a tool with `capability_update` |

## Self-heal
- DataFrame error: read `stderr`, check the column exists with `print(graph.columns)`.
- `dftracer-utils` missing: the `fix` it prints.
- A question asked twice: make it a tool with `capability_update`.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A query users find useful: `pipeline_feedback(kind=process)` to turn it into a tool.
- An API quirk you hit: `lesson_add` with the error.
