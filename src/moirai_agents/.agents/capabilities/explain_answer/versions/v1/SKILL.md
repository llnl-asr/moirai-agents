---
name: explain_answer
description: Explain the answer of a computed answer - the write-up led by its first sentence, its check, the gaps - as a plain summary and folded details. Load when changing how the Query view explains an answer's answer.
---

Cross-references: [[query_explain]] [[prov_query]]

# The answer part of an answer's explanation

`explain_answer(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`. The shared rules, constraints, corner cases and corrector table are in [[query_explain]]; this skill holds only the answer part.

## Rules
- The summary is the write-up's first sentence; the write-up's tables and bullets become blocks; then the synthesis check and the gaps as a table.
- Without a write-up, the summary says the computed result is the answer.
- Everything else (typed blocks, formatting, deterministic): see [[query_explain]].

## Constraints
- As in [[query_explain]] (numbers from the answer, summary under 320 characters, at most 4 facts).

## Corner cases
- As in [[query_explain]] (older answers, long cells).

## Corrector
Validator `explain_answer_check`; failures follow the table in [[query_explain]].

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here becomes a capability: `capability_builder`, Rules.
- An answer the user found hard to read: record a lesson here with the answer id and what was unclear.
