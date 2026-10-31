---
name: pipeline_control
description: Drive the run and analysis pipelines stage by stage with a user gate after each. Load whenever a pipeline is started or continued.
---

# Pipeline control

`run`: plan, explore, smoke, discover, annotate, integrate, moirai_smoke, baseline, graph, query, package. `analysis`: plan, ingest, dashboard, query. Each stage is owned by one
capability; read its skill before running it.

## Rules
- Plan first: the `plan` stage comes before anything else; load `run_planner`. When reality differs from the plan, `plan_update` at once: a stale step cannot pass.
- Call `pipeline_status` first. Work only on the current stage.
- Each stage: predictor, validator, then `ask_user` with `pipeline` and `stage` set, showing what was produced. Then end the turn.
- A gate is read at a glance. `question` is the decision in one sentence. `context` is one line of the result, then bullets: what changed, what is wrong, what happens next. Everything else goes in `details` as Markdown sections, bullets and tables. Name each item once; never write a paragraph.
- Mark what you advise with `recommended`, on one option. In auto mode `ask_user` takes it at once and records the gate as auto mode's answer: when the result says `answered by auto mode`, do not end your turn; `pipeline_advance` and go on. It still stops for you when nothing is recommended, when the stage's checks did not pass, or when the recommendation is to stop.
- Scale is the user's: in auto mode, recommend only what is within what they already approved.
- The gate's answer arrives as a message saying approved, process feedback or remember. Record it with `pipeline_decide` or `pipeline_feedback`.
- Before each gate, capture what the stage explored: anything new becomes a capability (`capability_builder`, Rules), and the gate says which ones were proposed.
- Advance only after the user approved. `pipeline_advance` refuses otherwise; do not look for a way around it.

## Constraints
- Never call `pipeline_decide(approved=true)` without the user's own words.
- An edit to a stage's outputs invalidates its verdict: validate again.
- Do not start a stage before the earlier ones passed.

## Corner cases
- A run pipeline has a dftracer session from `pipeline_new`: its work is under `<session>/provenance`, its app under `<session>/source`, and any `mcp__dftracer__session_*` tool takes its `run_id`.
- `session_error` from `pipeline_new`: fix it before going on; without a session the pipeline falls back to its own folder, which is not the same as working in the session.
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
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Feedback of kind `process` must end in a capability change or an agreed `no_change`.
- Something the pipeline taught you that the user did not say: `lesson_add` with the evidence; the user approves it.
