---
name: lessons
description: Record and find lessons. Load before recording anything, and before an unfamiliar task, to look up what is already known.
---

# Lessons

## Rules
- A lesson is settled knowledge: symptom, root cause, exact fix; or a rule the user stated, in their words.
- Attach it to the capability it concerns, so the agent that needs it reads it.
- `knowledge_search` comes before acting on anything unfamiliar; `lesson_search` looks at lessons only.

## Constraints
- No source, no lesson: `evidence` names a run, a log line, file:line, or the user's words.
- Only the user approves agent-proposed lessons.
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
