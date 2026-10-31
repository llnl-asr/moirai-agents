---
name: knowledge
description: Find what is known and remember what is true. Load before an unfamiliar task, an unfamiliar error, or recording a fact.
---

# Knowledge

One knowledge graph, built with graphify, covers every capability, lesson and memory, and dftracer-agents' workload, software, system and tools skills and its memories. Each prompt arrives with the best new rows in a `<moirai-context>` block.

## Rules
- `knowledge_search` comes first, before any other way of looking: before `capability_list`, before reading a tree, before trying something unfamiliar.
- Put the concrete words in the question: the error text, the tool, the application, the system.
- Read the `graph` rows first, then open only the sections you need with `knowledge_read`. Follow `[[links]]` and Cross-references the same way.
- Reformulate rather than widen: a miss means the words were wrong, so try the name of the thing.
- A hit under `dftracer/` or `dftracer-memory/` is used as it is: load that skill or read that memory; do not copy it.
- `<moirai-context>` is a starting point, not the answer: search again in your own words for what the task needs.
- Where a fact goes: how to do something, a capability; a verified fix, `lesson_add`; what is true here (a decision, a constraint, guidance), `memory_write`.

## Constraints
- Never write a memory for what the code or a capability already records.
- A `feedback` memory needs its Why and How to apply.
- One fact per memory; writing the same name updates it.

## Corner cases
- The graph matches headings and symbols, not body text, so a good heading is what makes knowledge findable. `lessons` and `memory` rows come from direct reads and cover body text.
- The graph rebuilds itself when anything it covers changed, so something just recorded is findable on the next search.
- Without graphify the search still answers from lessons and memory and sets `graph_error`.

## Corrector
Predictor `knowledge_search`, `memory_write`, validator `knowledge_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`graph` failed: install graphify with `scripts/setup_env.sh`. A capability or memory is missing from the graph: the stage is stale, so search once to rebuild it, then validate.

## Self-heal
- A tool here failed: follow `capability_builder`'s Self-heal.
- `graph_error` with a timeout: lower `budget` or narrow the question.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A search that missed what existed: fix the heading or description that should have matched, with `capability_update`.
