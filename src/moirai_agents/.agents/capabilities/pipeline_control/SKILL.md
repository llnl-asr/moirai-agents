---
name: pipeline_control
description: Drive the run and analysis pipelines stage by stage with a user gate after each. Load whenever a pipeline is started or continued.
---

# Pipeline control

`run`: explore, smoke, annotate, integrate, moirai_smoke, baseline, query. `analysis`: ingest, dashboard, query. Each stage is owned by one
capability; read its skill before running it.

## Rules
- Call `pipeline_status` first. Work only on the current stage.
- Each stage: predictor, validator, then `ask_user` with `pipeline` and `stage` set, showing what was produced. Then end the turn.
- The gate's answer arrives as a message saying approved, process feedback or remember. Record it with `pipeline_decide` or `pipeline_feedback`.
- Advance only after the user approved. `pipeline_advance` refuses otherwise; do not look for a way around it.

## Constraints
- Never call `pipeline_decide(approved=true)` without the user's own words.
- An edit to a stage's outputs invalidates its verdict: validate again.
- Do not start a stage before the earlier ones passed.

## Corner cases
- The user may skip the gate's buttons and answer in plain words: that is still their answer; quote it.
- Feedback that is both a fact and a process change: record `remember` for the fact and `process` for the change.
- A pipeline resumed in a later chat: `pipeline_status` holds everything needed.

## Corrector
Predictor `pipeline_new`, `pipeline_advance`, validator `pipeline_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`pipeline_validate` names the stage. A stale verdict: run that stage's validator again. A missing approval: ask the user; never record it for them. Open feedback: act on it, then `pipeline_feedback_resolve`.

## Self-heal
- A tool here failed: follow `capability_builder`'s Self-heal.
- `pipeline_status` says the stage is `failed`: read `failed`, fix with the owner's tool, validate again.
- A job shows `lost`: its process died with no exit code. Read the log tail, then re-run the stage.

## Self-learn
- Feedback of kind `process` must end in a capability change or an agreed `no_change`.
- Something the pipeline taught you that the user did not say: `lesson_add` with the evidence; the user approves it.
