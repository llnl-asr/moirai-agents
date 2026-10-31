---
name: run_planner
description: Plan a pipeline before it runs and keep the plan true while it runs. Load at the start of every pipeline, and whenever reality differs from the plan.
---

# Planner

The plan is the first stage of every pipeline. Each stage's owner reads its own step before starting.

## Rules
- Plan first: read the app or the traces, `knowledge_search` for what is known, then `plan_write` with a step for every stage.
- `intent` says what the stage produces for this app; `approach` says how; `done_when` is a test someone could check, beyond the validator passing; `risks` names what may go wrong here.
- Name real things: the build system, the likely artifact, the scale, the queue. A plan that fits any app is not a plan.
- The engine records each stage's status and result. You record why things changed: `plan_update` the moment the plan stops being true.
- Show the plan at the gate, and again when a revision changes what the user approved.

## Constraints
- Do not run a stage from here: the planner only writes.
- Never rewrite history: `plan_update` keeps every revision; use `plan_write` only before work starts.
- A stale step blocks its stage from passing. Clear it by saying what changed, never with an empty reason.
- Scale, time and cost in the plan are proposals until the user approves them.

## Corner cases
- Unknowns are normal before explore: record them as assumptions or questions, then revise after explore.
- The user changes the goal mid-run: `plan_update` each affected step and say so at the next gate.
- A stage skipped by agreement: say why in `plan_update`.

## Corrector
Predictors `plan_write`, `plan_update`, validator `plan_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `written`, `file` | run `plan_write` |
| `covers_stages` | add the missing stages |

## Self-heal
- `plan_update` is refused: the stage name is wrong; `plan_show` lists them.
- A step looks out of date but is not stale: only failures and feedback mark it; update it anyway.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A risk that came true and was not planned for: `lesson_add` on the stage's capability, so the next plan names it.
