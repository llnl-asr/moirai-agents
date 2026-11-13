---
name: query_explain
description: Explain a computed answer for the Query view in four parts - plan, result, answer, evidence - each a high-level summary first with details folded and formatted (tables, queries, records, numbers); each part is its own capability. Holds the rules shared by all four parts. Load when the user finds an answer hard to read or asks how an answer was explained.
---

Cross-references: [[explain_plan]] [[explain_result]] [[explain_answer]] [[explain_evidence]] [[prov_query]]

# Query explanation

The Query view shows a computed answer (evidence_answer) as four parts. `query_explain(pipeline, answer_id)` runs one capability per part,
`explain_<part>(answer)`, each returning `{part, title, summary, facts, details: [{title, pick?, blocks}]}`. The view shows the summary and
facts of each part and folds the details; `pick` (s1, s2, ...) ties a detail group to its step on the graph. Blocks and their shapes: `core/explain.py`.
The Rules, Constraints and Corner cases below are shared by explain_plan, explain_result, explain_answer and explain_evidence; those skills hold only what is specific to their part.

## Rules
- High level first: a part's summary is one or two plain sentences a domain scientist reads without the details.
- Details carry everything else, typed blocks (text, kv, table, query, stats, list, note): a table is a `table` block (never markdown in text), a query is a `query` block, several values are a list (chips), name -> value is `kv`. One way to show each kind of thing everywhere.
- Format values with `core.explain`: `fmt_num` / `fmt_value` (thousands separators, epoch seconds and microseconds as UTC times), `split_values` for "a | b" and "a,b".
- Each part is its own capability: an unclear plan is fixed in explain_plan, a badly shown table in explain_result, and so on.
- `query_explain_validate` after changing any part.

## Constraints
- No model, deterministic: every word and number comes from the answer record.
- Every number in a part's summary and facts is in the answer or a count of it (`explain.check`).
- Summary under 320 characters; at most 4 facts.
- A part that fails is reported in `problems`, never filled in by hand.

## Corner cases
- Older answers (no engine, text gaps, no steps): say so in the summary, keep the details empty rather than inventing.
- Long cells: keep them; the view clips and expands them. Very long results: tables are cut at 200 rows with the total stated.

## Corrector
Validator `query_explain_validate` (runs each part's `explain_<part>_check`).

| Failed check | Correct |
|---|---|
| a part fails its check | fix that part's capability (capability_update explain_<part>), then validate again |
| a part raised | read the problem; the answer record may lack a field the part needs: handle it in that part |
| summary too long or has a table | move it to details as a block |
| a number not in the answer | state counts of what the answer holds, or drop the number |
| unknown block / bad table row | use the block shapes in `core/explain.py` |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- What the user found unclear in an answer: a lesson on the part's capability (explain_<part>), with the answer id and what was unclear.
