---
name: prov_traceability
description: Traceability aspect of provenance - where did this come from: workflow and app instance, process, function, code region, parameters, attributes. Load when a question or a check is about where something came from, its lineage, the parameters or the code that made it, and when annotating so the run records it.
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] [[prov_gaps]]

# Traceability

Scope: Where did this come from: the workflow instance and app instance, the process and the function or code region that made it, the parameters (inputs) it was run with, what it returned (outputs), and the attributes of the entities, followed back to the roots (the inputs nothing in the traces produced) or forward to everything they fed.

## Rules
- Answer a user's question with `evidence_answer` and these operations (each step is a TraceViewer query through prov_read; the answer is kept with the trace):
  - `entities`, `activities`, `lineage` (back to the roots, forward to what it fed), `ends`, `group`, `inputs` (parameters), `outputs`, `attributes`
- `traceability_summary` gives the standard picture of a run; `traceability_check` says what the annotation did not capture, with the fix.
- What the annotation must record for this aspect: every function that reads or writes a scientific artifact as an activity with used/generated entities, its parameters as capture.inputs, its results as capture.outputs, the facts of each entity as attributes, and the workflow/app instance as entities (prov_annotate spec).
- The roots of a backward lineage are where something came from; an entity nothing produced that is not an input is a missing_producer gap.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns traceability only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Workflow and app instance are entities (pipeline_task, the app run): never inferred from file names alone.

## Corner cases
- A subprocess or callback the decorator does not reach shows as missing_producer: the fix is in prov_gaps / prov_annotate (missed_function).
- Identical values share one attribute entity: count per entity it is part_of.

## Corrector
Validator `traceability_check` (and `traceability_selftest` for the capability itself).

| Failed check | Correct |
|---|---|
| `entities_declared` | declare the artifacts as entity types in the spec (prov_annotate) |
| `activities_recorded` | annotate the functions that read or write them as activities |
| `parameters_captured` (advice) | capture.inputs on each activity |
| `attributes_captured` (advice) | entity attributes in the spec |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.traceability with its selftest.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of traceability question that needed a hand-built plan becomes an operation in core.aspects.traceability with a selftest case.
