---
name: explain_answer
description: Explain the answer of a computed answer - the write-up led by its first sentence, its check, the gaps - as a plain summary and folded details. Load when changing how the Query view explains an answer's answer.
---

Cross-references: [[query_explain]] [[prov_query]]

# The answer part of an answer's explanation

`explain_answer(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`; the Query view shows the
summary and the facts, and folds the details. Blocks and their shapes: `core/explain.py`.

## Rules
- High level first: the summary says in one or two plain sentences what this part is: the write-up's first sentence; tables and bullets of the write-up as blocks; the synthesis check; the gaps as a table.
- Details carry everything else, typed: a table is a `table` block (never markdown in text), a query is a `query` block, several values are a list (chips), name -> value is `kv`.
- Format values with `core.explain`: `fmt_num` / `fmt_value` (thousands separators, epoch seconds and microseconds as UTC times), `split_values` for "a | b" and "a,b".
- Deterministic: no model, every word from the answer record.

## Constraints
- Every number in the summary and facts is in the answer or a count of it (`explain.check`).
- Summary under 320 characters; at most 4 facts.

## Corner cases
- An older answer (no engine, text gaps, no steps): say so in the summary, keep details empty rather than inventing.
- Long cells: keep them; the view clips and expands them.

## Corrector
Validator `explain_answer_check`.

| Failed check | Correct |
|---|---|
| summary too long or has a table | move it to details as a block |
| a number not in the answer | state counts of what the answer holds, or drop the number |
| unknown block / bad table row | use the block shapes in `core/explain.py` |

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- An answer the user found hard to read: record a lesson here with the answer id and what was unclear.
