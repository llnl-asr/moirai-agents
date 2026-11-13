---
name: explain_result
description: Explain the result of a computed answer - how much was found and what differs between runs - as a plain summary and the result as a formatted table (values as chips, numbers and times formatted). Load when changing how the Query view explains an answer's result.
---

Cross-references: [[query_explain]] [[prov_query]]

# The result part of an answer's explanation

`explain_result(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`. The shared rules, constraints, corner cases and corrector table are in [[query_explain]]; this skill holds only the result part.

## Rules
- The summary says in one or two plain sentences what was found: the first clause of the headline, the table's size, the values that differ between runs; the table itself as a table block.
- Everything else (typed blocks, formatting, deterministic): see [[query_explain]].

## Constraints
- As in [[query_explain]] (numbers from the answer, summary under 320 characters, at most 4 facts).

## Corner cases
- As in [[query_explain]] (older answers, long cells).

## Corrector
Validator `explain_result_check`; failures follow the table in [[query_explain]].

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here becomes a capability: `capability_builder`, Rules.
- An answer whose result the user found hard to read: record a lesson here with the answer id and what was unclear.
