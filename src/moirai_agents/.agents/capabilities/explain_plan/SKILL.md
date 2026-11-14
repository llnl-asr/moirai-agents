---
name: explain_plan
description: Explain the plan of a computed answer - what was asked of the traces, step by step - as a plain summary and folded details (arguments, time window, TraceViewer queries, records cited). Load when changing how the Query view explains an answer's plan.
---

Cross-references: [[query_explain]] [[prov_query]]

# The plan part of an answer's explanation

`explain_plan(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`. The shared rules, constraints, corner cases and corrector table are in [[query_explain]]; this skill holds only the plan part.

## Rules
- The summary says in one or two plain sentences what was asked: the steps in words; per step a kv of its arguments and a query block per TraceViewer query.
- Everything else (typed blocks, formatting, deterministic): see [[query_explain]].

## Constraints
- As in [[query_explain]] (numbers from the answer, summary under 320 characters, at most 4 facts).

## Corner cases
- As in [[query_explain]] (older answers, long cells).

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Validator `explain_plan_check`; failures follow the table in [[query_explain]].

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here becomes a capability: `capability_builder`, Rules.
- An answer whose plan the user found hard to read: record a lesson here with the answer id and what was unclear.
