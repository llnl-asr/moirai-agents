---
name: prov_graph
description: Build the provenance graph of the baseline run, diff the aggregated entity-relationship graph the annotation promised against the one recorded, run the graph inference algorithms (none yet), and gate on its health. Load for the graph stage.
---

Cross-references: [[prov_annotate]] [[prov_discover]] [[tools_moirai]] [[prov_query]]

# Graph stage

The graph stage is where the Annotate view shows the user whether the annotation works: the aggregated entity-relationship
graph the spec promised, diffed against the one the baseline recorded (`health.diff`; the view draws matched, missing,
conditional and unexpected). Besides the diff it validates the graph and runs the graph inference algorithms. There are no inference algorithms yet (`INFERENCES` in tool.py is empty), so today it only validates.
The next stage is `package`. Questions about the graph are not a stage: the user asks them in the Workbench's Query view,
answered with `prov_query`'s `evidence_answer` (TraceViewer queries, computed results).

## Rules
- `graph_check`, then `graph_validate`. Done means all of it holds: provenance recorded, no dangling id, every artifact type present with a producer, every root a real input agreed in the spec, every type described, and no inference algorithm failed.
- At the gate, report the diff first (matched / missing / conditional / unexpected, in words), then counts by type and one full lineage example. The user judges conditional and unexpected ones.
- Run the tools in the stage agent or in the main thread, not both: `graph_check` reads every trace record once, and a second reader doubles the wait.
- A new graph inference algorithm is a `(name, fn(graph, spec) -> dict)` entry in `INFERENCES`, with a check in checks.json; its result lands in `health.inferences[name]`.

## Constraints
- Never call a graph healthy from counts alone: the validator decides.
- Every artifact instance has a producer activity, and its ancestors reach root inputs. The graph stays a DAG.
- Do not change the spec here to make the gate pass; fix the annotation, or ask.
- Do not compare the plain and traced run's outputs here: that is not this stage (v2 did; see versions/v2).

## Corner cases
- A partial trace set shows up as dangling ids.
- A worker that dies without finishing loses its open events; check the process's exit path.
- A large campaign (millions of records) takes minutes on the first `graph_check`; later calls in the same server read only what is new.

## Corrector
Predictor `graph_check`, validator `graph_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `recorded` | the annotated functions did not run, or the app ran outside the annotated tree |
| `no_dangling` | a process's trace is missing, or a related entity is never declared |
| `artifacts_present` | annotation gap at the writer, or the input does not reach it |
| `artifacts_produced` | add `generated` at the writer |
| `roots_are_inputs` | a producer upstream is not annotated: go back to `prov_annotate` |
| `types_described` | fix the spec's descriptions, then `annotate_apply` |
| `expected_recorded` | a spec edge or type was never recorded: the baseline input did not reach that code, or the annotation there is wrong (`prov_annotate`) |
| `inferences_ran` | an algorithm in `INFERENCES` raised: fix it with `capability_update`, then `graph_check` again |

## Self-heal
- `graph_check` has no traces: the baseline did not finish.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A gap the gate missed: `capability_update` with a check for it.
- When a pipeline is confirmed, add its case to `prov_discover`'s `CASES.md`.
