---
name: prov_graph
description: Build the provenance graph of the baseline run and gate on its health - every artifact produced and rooted in real inputs. Load for the graph stage.
---

# Graph health gate

## Rules
- `graph_check`, `graph_compare`, `graph_validate`. Done means all of it holds: provenance recorded, no dangling id, every artifact type present with a producer, every root a real input agreed in the spec, every type described.
- At the gate, report entity and activity counts by type and one full lineage example, root to artifact to consumers.
- Verify the run did not change the science: compare artifacts between the plain and the traced run.

## Constraints
- Never call a graph healthy from counts alone: the validator decides.
- Every artifact instance has a producer activity, and its ancestors reach root inputs. The graph stays a DAG.
- Do not change the spec here to make the gate pass; fix the annotation, or ask.

## Corner cases
- A file differs only by a timestamp or a run id: that is harmless, but it is the user's call, so the check is inconclusive, not failed.
- A partial trace set shows up as dangling ids.
- A worker that dies without finishing loses its open events; check the process's exit path.

## Corrector
Predictors `graph_check`, `graph_compare`, validator `graph_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `recorded` | the annotated functions did not run, or the app ran outside the annotated tree |
| `no_dangling` | a process's trace is missing, or a related entity is never declared |
| `artifacts_present` | annotation gap at the writer, or the input does not reach it |
| `artifacts_produced` | add `generated` at the writer |
| `roots_are_inputs` | a producer upstream is not annotated: go back to `prov_annotate` |
| `types_described` | fix the spec's descriptions, then `annotate_apply` |

## Self-heal
- `graph_check` has no traces: the baseline did not finish.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A gap the gate missed: `capability_update` with a check for it.
- When a pipeline is confirmed, add its case to `prov_discover`'s `CASES.md`.
