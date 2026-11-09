---
name: answer_confidence
description: The confidence of an answer, 0-100 - how much of it was calculated by TraceViewer queries on the traces versus inferred by the LLM (or missing, a gap) - with every LLM-inferred part flagged. Load when showing, judging or improving an answer, and when the user asks how sure an answer is.
---

Cross-references: [[query_explain]] [[prov_query]] [[prov_gaps]]

# Answer confidence

`confidence_score(pipeline, answer_id)` splits an answer into parts - each plan step, the narrative, each sentence of the agent's write-up,
each evidence item of an agent-written answer, each gap - and says how each was obtained:

| how | weight | meaning |
|---|---|---|
| query | 1.0 | a TraceViewer query on the traces |
| derived | 0.9 | computed from an earlier step, no new query |
| narrative | 1.0 | written from the computed result, values linked and checked |
| llm_verified | 1.0 | LLM wording of query values: every value it states is in the query results (the cited step's, or any step's) |
| wording | not counted | LLM wording without any value |
| llm_query | 0.3 | a query the agent ran itself and summarized, not recomputed |
| llm | 0 | a value the LLM derived: no query computed it (flagged, with the value) |
| gap | 0 | not shown by the provenance |

The score is the weighted share, halved when the traces no longer give the result. The Query view shows it on every answer.

## Rules
- What counts is where values come from: every value must come from a query; the LLM may word and arrange them, and each value it states is validated. A value no query computed is LLM-derived.
- More calculated is better: to raise a score, answer with evidence_answer (TraceViewer steps) instead of writing evidence by hand, cite steps in a write-up, and close gaps (prov_gaps).
- Every LLM-written part is flagged `llm: true` with why; never hide one.
- `confidence_validate` after any change to the weights or the items.

## Constraints
- Deterministic: from the answer record and the traces, no model.
- Weights change only with a reason in the capability history.

## Corner cases
- Agent-written answers (no engine): nothing is calculated here; their queries were the agent's own and are not recomputed (llm_query).
- A write-up sentence with a number the result does not have is inferred (llm), even if it cites a step.

## Corrector
Validator `confidence_validate`.

| Failed check | Correct |
|---|---|
| a step, sentence, evidence item or gap is not an item | add it in `items` |
| an LLM part not flagged | set llm and why |
| the score is not the weighted share | compute it with `score_of` |
| could not be checked against the traces | the pipeline has no traces, or the plan no longer runs: ask again |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- An answer the user judged differently from its score: a lesson here with the answer id and why.
