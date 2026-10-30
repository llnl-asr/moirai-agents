---
name: capability_builder
description: Build or change a capability (MCP tool + skill + scoped agent). Load before capability_new or capability_update, for process feedback, and to repair a failing tool.
---

# Building capabilities

A capability is one folder: `tool.py` (what has one right answer), `SKILL.md` (what needs judgement), `agent.json` (who runs it, with what
scope), `checks.json` (known input to expected output), `LESSONS.md`. Read LESSONS.md before you start.

## Rules
- If two people would do it identically, it is code in `tool.py`. Prose carries only what needs judgement.
- `capability_list` first. Extend what exists; a fingerprint twin is refused.
- Every capability is a predictor-validator-corrector loop. Declare `pvc`. The validator is a separate deterministic tool returning a verdict (`finish_validation` for stages); a tool never judges its own output. The skill's `## Corrector` maps each failed check to the fix of the original tool.
- A failed assumption is `Verdict.assume`: inconclusive, never a failure.
- A verdict names each fault and the fix, so nobody has to diagnose it again.
- Docstrings are the only description an agent sees: what it does, when to use it, each argument, the return keys.

## Constraints
- A person approves every proposal. Call `capability_apply` only after they said yes, with `confirmed=true`.
- Never edit capability files directly: edits skip validation and the human gate.
- No `eval`, `exec`, `shell=True`, `os.system`. No machine-specific paths: take them as arguments.
- `agent.json` declares `level_1|2|3`, never a model. Bash must be scoped, for example `Bash(git diff:*)`.
- The skill needs all of: Rules, Constraints, Corner cases, Self-heal, Self-learn.

## Corner cases
- Tool names are global: a name another capability owns is refused.
- Checks run with an empty HOME and no network assumptions: test logic on data the check creates, not on a site's files.
- Updating a bundled capability creates a shadow copy in a writable layer; its lessons come with it.

## Corrector
Predictor `capability_new`, `capability_update`, validator `capability_verify`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`capability_verify` lists each problem. Bundle problems name the file to change; a failed check means the tool or the check is wrong: reproduce it, then decide which. Fix with `capability_update`, never by loosening the check to pass.

## Self-heal
- Tool failed: `capability_get`, reproduce it with a new check, `capability_update`, ask the user, `capability_apply`, re-run the stage.
- `load error` in `capability_list`: the last working tools still serve; fix the error the same way.
- An update made it worse: `capability_rollback`. No quick fix: `capability_disable` and tell the user.

## Self-learn
- A mistake in how capabilities are built becomes a lesson here with `lesson_add`, citing the failed proposal or check.
