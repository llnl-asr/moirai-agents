---
name: query_explain
description: Explain a computed answer for the Query view in four parts - plan, result, answer, evidence - each a high-level summary first with details folded and formatted (tables, queries, records, numbers); each part is its own capability. Load when the user finds an answer hard to read or asks how an answer was explained.
---

Cross-references: [[explain_plan]] [[explain_result]] [[explain_answer]] [[explain_evidence]] [[prov_query]]

# Query explanation

The Query view shows a computed answer (evidence_answer) as four parts. `query_explain(pipeline, answer_id)` runs one capability per part,
`explain_<part>(answer)`, each returning `{part, title, summary, facts, details: [{title, pick?, blocks}]}`. The view shows the summary and
facts of each part and folds the details; `pick` (s1, s2, ...) ties a detail group to its step on the graph.

## Rules
- High level first: a part's summary is one or two plain sentences a domain scientist reads without the details.
- Details are typed blocks (core/explain.py): text, kv, table, query, stats, list, note. One way to show each kind of thing everywhere.
- Each part is its own capability: an unclear plan is fixed in explain_plan, a badly shown table in explain_result, and so on.
- `query_explain_validate` after changing any part.

## Constraints
- No model: every word and number comes from the answer record.
- A part that fails is reported in `problems`, never filled in by hand.

## Corner cases
- Older answers (no steps, text gaps): the parts say so and keep the details empty.
- Very long results: tables are cut at 200 rows with the total stated.

## Corrector
Validator `query_explain_validate`.

| Failed check | Correct |
|---|---|
| a part fails its check | fix that part's capability (capability_update explain_<part>), then validate again |
| a part raised | read the problem; the answer record may lack a field the part needs: handle it in that part |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- What the user found unclear in an answer: a lesson on the part's capability, with the answer id.
