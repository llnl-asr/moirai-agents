---
name: explain_answer
description: Explain the answer of a computed answer - a checked write-up as the answer (its first sentence the summary), else the narrative computed from the result; the gaps - as a plain summary and folded details. Load when changing how the Query view explains an answer's answer.
---

Cross-references: [[query_explain]] [[prov_query]]

# The answer part of an answer's explanation

`explain_answer(answer)` turns a computed answer (an answers.jsonl record) into `{part, title, summary, facts, details}`. The shared rules, constraints, corner cases and corrector table are in [[query_explain]]; this skill holds only the answer part.

## Rules
- Always a narrative that answers the question: the direct answer first, then what differs and what stays the same, then how it was found. Never point at the result instead of answering ("No write-up: the computed result is the answer" is refused by the validator).
- The first sentence answers in the question's own words: "What is the quality of the MSAs?" opens "The quality of the MSAs is mixed: ...". A how-good question with a known measure of quality (QUALITY in tool.py: HHsearch probability, E-value, pLDDT, coverage, identity) gets a verdict - good, mixed or poor by how many items reach the measure's level - and the level is said, so the reader can disagree. Otherwise the lead measure's median and range.
- Numbers come from every item, not the distinct values: each measure's median, min and max (the step table's columns) and each item's own values (the step's `per` table). Then a table of each item's values, best first, joined across steps by folder when an item's records share one (an MSA and its template search).
- Lead with the measure the question names; else the known quality measures in QUALITY's order; then the rest (at most 4 in the text, 6 in the table).
- Link every claim to where it can be checked: [R n] is row n of the result table, [S n] is step n. Quote values in backticks: the validator looks each one up in the row or step it links.
- A write-up the synthesis check passed IS the answer: it reads like the agent's reply in the chat (the direct answer, the spread, the coverage, how it was computed, the caveats) with its [S n] citations to the data. Its first sentence is the summary, the rest follows as blocks (tables and bullets as blocks), then the steps it cites; the narrative computed from the result goes in the details. A write-up the check flagged goes in the details with the problems, and the computed narrative leads.
- A stats result (a value's mean over every entity that records it) is told directly: the mean over how many, the median and range, and how many values are distinct.
- Everything else (typed blocks, formatting, deterministic): see [[query_explain]].

## Constraints
- As in [[query_explain]] (numbers from the answer, summary under 320 characters, at most 4 facts).

## Corner cases
- As in [[query_explain]] (older answers, long cells).
- An answer computed before steps had medians and `per` tables: told the old way (what differs, what is the same); ask again for the direct answer.
- A probability recorded as 0-1: the level is scaled from 0-100.

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Validator `explain_answer_check`; failures follow the table in [[query_explain]].

## Self-heal
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here becomes a capability: `capability_builder`, Rules.
- An answer the user found hard to read: record a lesson here with the answer id and what was unclear.
- 2026-11-10: the user found the chat reply (direct answer, spread, coverage, method, caveats) better than the panel's narrative, which hid the write-up in the details: a checked write-up now leads.
