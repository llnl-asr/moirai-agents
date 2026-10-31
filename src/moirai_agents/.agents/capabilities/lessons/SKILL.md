---
name: lessons
description: Record and find lessons. Load before recording anything, and before an unfamiliar task, to look up what is already known.
---

# Lessons

## Rules
- A lesson is settled knowledge: symptom, root cause, exact fix; or a rule the user stated, in their words.
- Attach it to the capability it concerns, so the agent that needs it reads it.
- `knowledge_search` comes before acting on anything unfamiliar. `lesson_search` searches lessons only, through the graph: use it with the error text the moment something fails, before exploring on your own.
- A failed tool result already carries `known_lessons`: read them first; a graph row opens with `knowledge_read`.

- Consolidating: `consolidation_report`, then read each group's members. Overlapping lessons: `lesson_merge` into one, in the single best home, keeping every fact. Lessons that contradict each other: ask the user which holds, before proposing anything. Overlapping capabilities: `capability_update` the better home, then ask the user whether to `capability_disable` the other. A group that is different on purpose: `consolidation_keep` with why.
- Consolidation is reading and proposing: it runs on the web server's node, and never needs a job or a compute node.

## Constraints
- No source, no lesson: `evidence` names a run, a log line, file:line, or the user's words.
- Only the user approves agent-proposed lessons. They accept, reject or refine each one in the Lessons panel.
- A refinement the user asks for goes through `lesson_revise`, then ask them to accept it; do not add a second lesson.
- One copy of each fact: a near-duplicate is refused; do not reword it to get past that.

## Corner cases
- If the fix can be a rule in code, it is a capability update, not a lesson.
- Lessons on bundled capabilities are stored in the user layer, shadowing the bundled folder.

## Corrector
Predictor `lesson_add`, validator `lesson_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`lesson_validate` names the lesson id. Gone capability: move or reject it. Missing from LESSONS.md: `lesson_add` again. No evidence: reject it.

## Self-heal
- Conflicting lessons: ask the user which holds, then reject the other.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A lesson that turned out wrong: tell the user and record the correction.
