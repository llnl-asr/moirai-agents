---
name: trace_ingest
description: Take provenance traces from the user and check what they hold. Load for the ingest stage of an analysis pipeline.
---

# Ingest

## Rules
- Ask for the trace location if it is not given. `ingest_traces`, then say at the gate how many files, activities and entities, and which run it seems to be (read the README next to the traces if there is one).

## Constraints
- Traces are read-only; indexes go in the pipeline directory.
- Large trace sets: this reads every file once. For more than a few GiB, ask before running on a login node.

## Corner cases
- Traces from dftracer's former provenance API use category `PROV`; they read the same.
- One entity has `EH` records in several files: they merge.
- A partial set of a run shows up as dangling ids.

## Corrector
Predictor `ingest_traces`, validator `ingest_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `files` | ask for the path; quote globs |
| `provenance` | these are not moirai traces: tell the user |
| `no_dangling` | part of the set is missing: ask for the rest |

## Self-heal
- `files` failed: a glob needs quoting, or the path is on a file system this node cannot see.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A trace layout the tool missed: `capability_update` with a check for it.
