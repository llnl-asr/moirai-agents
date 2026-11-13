---
name: explain_evidence
description: Explain the evidence of a computed answer - engine, files, queries, records read and cited, result hash - as a plain summary and folded tables of files, queries and cited records. Load when changing how the Query view explains an answer's evidence.
---

Cross-references: [[query_explain]] [[prov_query]]

# The evidence part of an answer's explanation

`explain_evidence(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`. The shared rules, constraints, corner cases and corrector table are in [[query_explain]]; this skill holds only the evidence part.

## Rules
- The summary says in one or two plain sentences how the answer was computed; files, queries and cited records as table and query blocks.
- Everything else (typed blocks, formatting, deterministic): see [[query_explain]].

## Constraints
- As in [[query_explain]] (numbers from the answer, summary under 320 characters, at most 4 facts).

## Corner cases
- As in [[query_explain]] (older answers, long cells).

## Corrector
Validator `explain_evidence_check`; failures follow the table in [[query_explain]].

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here becomes a capability: `capability_builder`, Rules.
- An answer whose evidence the user found hard to read: record a lesson here with the answer id and what was unclear.
