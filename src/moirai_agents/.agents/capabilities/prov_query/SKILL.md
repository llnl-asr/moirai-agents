---
name: prov_query
description: Answer provenance questions - lineage, downstream impact, what happened when - from the traces through dftracer-utils' TraceViewer, with evidence: the question becomes a plan of concrete steps, each a TraceViewer query, the result is computed, and the write-up cites the steps. Load for the analysis pipeline's query stage and for any question in the Query view of a run, live run or dataset.
---

Cross-references: [[prov_graph]] [[prov_dashboard]] [[live_inputs]]

# Query: evidence-backed answers

An answer is computed, never written: the question is resolved into concrete entities, activities and time frames, each step is a
TraceViewer query on the actual traces, and the result comes from the records. Your only parts are the plan and, after the result,
a short write-up that cites it. The Query view shows the plan, every query, the files read, the cited records, the lineage graph and
the result; the user can re-run it.

## Rules
- Resolve first: `query_summary` (types, activities, time span), `query_entities` to turn the user's words into real types and ids. Never guess a type name: the plan is refused when a type is not in the traces.
- Then `evidence_answer(pipeline, question, steps)` for every question about lineage, impact, counts, time or parameters. Steps: `entities`, `activities`, `lineage` (back = where it came from, forward = what it affected), `group`, `ends`, `inputs` (an activity's parameters: arguments, object attributes, command-line flags), `outputs` (what it returned), `attributes` (an entity's recorded attributes). Times: ISO, epoch, or `start+30m` / `end-2h`.
- "What parameters / settings / thresholds did X use" is `inputs` of the activity that does X (find its name with `query_summary`). When it says "none recorded", the app was annotated before inputs were captured: say so, and suggest annotating again.
- Read the computed results, then `evidence_synthesis(pipeline, id, text)`: a few sentences or bullets for the user, every claim citing its step as [S1]..[Sn], only numbers and names that are in the results. If the check flags anything, fix the text and call it again.
- In the chat, one line pointing at the answer in the Query view is enough; do not repeat the tables.
- `query_dataframe` is for ad-hoc exploration only; an answer to the user goes through `evidence_answer`.
- A live run's traces grow: `evidence_rerun` repeats a plan on the current traces and says whether the result changed.

## Constraints
- Never state a number, entity or time that is not in a step's result.
- Never edit an answer's result: a different question is a new `evidence_answer`.

## Corner cases
- A lineage that stops early: the result names ids without an entity record (dangling) - say so, it is an annotation gap.
- A very large run: the first query builds the TraceViewer index (about a minute for 10M records); later queries take seconds.
- Entity records carry no time: `produced_since`/`produced_until` select entities by the time of the activity that produced them.

## Corrector
Validator `query_validate` (the analysis pipeline's query stage).

| Failed check | Correct |
|---|---|
| `asked` | run the user's questions with `evidence_answer` |
| `graph` | the traces hold no provenance records: check the path and the annotation |
| synthesis flagged | cite an existing step, use the computed numbers, call `evidence_synthesis` again |

## Self-heal
- A plan is refused: the error names the step and the fix (unknown type, bad `from`, missing selector).
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A question the step vocabulary could not express: record it as a lesson on prov_query, with the question and what was missing.
