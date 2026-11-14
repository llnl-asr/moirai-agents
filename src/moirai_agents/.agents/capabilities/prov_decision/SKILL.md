---
name: prov_decision
description: Decision making aspect of provenance - why trust the result: quality, quantity, metrics, what failed or passed. Load when a question or a check is about whether a result can be trusted, its quality or quantity, or what failed or passed, and when annotating so the run records it.
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] 

# Decision making

Scope: Why trust the result: quality metrics (scores, confidences, error estimates), quantity (how many items were produced, how many inputs each used), and what failed or passed (return and exit codes, status flags, errors, validation checks), so a result can be accepted or rejected on evidence.

## Rules
- Answer a user's question with `evidence_answer` and these operations (each step is a TraceViewer query through prov_read; the answer is kept with the trace):
  - `status` {names?}: what failed or passed: return/exit codes, status, ok/success/failed flags, errors, counted by value, failures named
  - `stats` {name, type?}: n, mean, median, min, max of a quality or quantity metric over every entity that records it
  - `attributes`, `outputs`: the values themselves
- `decision_summary` gives the standard picture of a run; `decision_check` says what the annotation did not capture, with the fix.
- What the annotation must record for this aspect: quality and quantity metrics as attributes of the entities they describe (e.g. mean_plddt=, n_seqs=), and every step's outcome as an output or attribute: returncode=, status=, ok=, error= (prov_annotate capture.outputs / attributes).
- A non-zero return code, a failed/false flag or a non-empty error is a failure; say which activity or entity it belongs to.
- Quality is judged on recorded metrics only (scores, confidences, error estimates), with their n.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns decision only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Never call a result trustworthy without the metric and its n; never call a step passed when no outcome is recorded (that is a gap).

## Corner cases
- A step can pass (returncode=0) and still produce poor output: report quality metrics next to the outcomes.
- Identical metric values share one entity: stats counts each once per entity it describes.

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Validator `decision_check` (and `decision_selftest` for the capability itself).

| Failed check | Correct |
|---|---|
| `outcomes_recorded` | capture each step's outcome as an output: capture.outputs returncode=, status=, ok=, error= (prov_annotate) |
| `quality_metrics_recorded` | record the metrics as attributes of the entities they describe |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.decision with its selftest.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of decision question that needed a hand-built plan becomes an operation in core.aspects.decision with a selftest case.
