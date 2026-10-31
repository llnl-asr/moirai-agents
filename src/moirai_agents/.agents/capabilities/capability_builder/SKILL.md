---
name: capability_builder
description: Build or change a capability (MCP tool + skill + scoped agent). Load before capability_new or capability_update, for process feedback, and to repair a failing tool.
---

# Building capabilities

A capability is one folder: `tool.py` (what has one right answer), `SKILL.md` (what needs judgement), `agent.json` (who runs it, with what
scope), `checks.json` (known input to expected output), `LESSONS.md`. Read LESSONS.md before you start.

## Rules
- If two people would do it identically, it is code in `tool.py`. Prose carries only what needs judgement.
- Anything explored becomes a capability, constantly: a new workload, software, tool or system is captured in the stage that met it, with what the run proved. dftracer-agents' skills are reused, not copied.
- `knowledge_search` first, then `capability_list`. Extend what exists; a fingerprint twin is refused. One subject per capability: do not widen one to fit.
- Every capability is a predictor-validator-corrector loop. Declare `pvc`. The validator is a separate deterministic tool returning a verdict (`finish_validation` for stages); a tool never judges its own output. The skill's `## Corrector` maps each failed check to the fix of the original tool.
- A failed assumption is `Verdict.assume`: inconclusive, never a failure.
- A verdict names each fault and the fix, so nobody has to diagnose it again.
- Docstrings are the only description an agent sees: what it does, when to use it, each argument, the return keys.

## Constraints
- A person approves every proposal: they approve, reject or refine it in the Capabilities panel. Call `capability_apply` only after they said yes, with `confirmed=true`.
- A refinement of a pending proposal goes through `capability_refine`, never a second `capability_new`.
- Never edit capability files directly: edits skip validation and the human gate.
- No `eval`, `exec`, `shell=True`, `os.system`. No machine-specific paths: take them as arguments.
- `agent.json` declares `level_1|2|3`, never a model. Bash must be scoped, for example `Bash(git diff:*)`.
- The skill needs all of: Rules, Constraints, Corner cases, Self-heal, Self-learn, and its Self-learn says what is explored there becomes a capability.

## What is learnt
The same families as dftracer-agents, with `_` where its skills use `-`:

| Family | For | Its skill holds |
|---|---|---|
| `workload_<app>` | a scientific or HPC application under study | build, run configuration, annotation pitfalls, what its provenance looks like |
| `software_<x>` | a library, a service, or a DL application that is instrumented rather than studied | versions that work, build, tuning, how it is traced |
| `tools_<x>` | a tool of this stack: moirai, dftracer, dfdashboard | install sources, what it really is, failure modes |
| `system_<x>` | a machine | key constraints, module load order, scheduler, storage, debugging |

If unsure between `workload_` and `software_`: is it studied, or instrumented? Ask if it is really ambiguous.

The skill follows dftracer-agents' shape, so its headings are what `knowledge_search` finds:
- The description says what it covers and when to load it, with the words someone would search.
- Directly under the frontmatter: `Cross-references: [[workload_x]] [[software-hdf5]]`, one link for each related capability or dftracer skill. It is required.
- After the required sections, topic sections: `## Build`, `## Run`, `## Scheduler`, each with one `### <the problem, in the words of the error>` per pitfall.
- Each pitfall gives the exact error, the cause, the exact fix and a command to verify it, and says `(confirmed YYYY-MM-DD)` with the run that proved it.
- Its tool holds what can be checked: a predictor that reads the facts (versions, modules, paths) and a validator that rules on them. The same rule as any capability.

Corrections and one-off fixes found later go to its LESSONS.md, not the skill.

## Corner cases
- Tool names are global: a name another capability owns is refused.
- Checks run with an empty HOME and no network assumptions: test logic on data the check creates, not on a site's files.
- Updating a bundled capability creates a shadow copy in a writable layer; its lessons come with it.

## Corrector
Predictor `capability_new`, `capability_update`, validator `capability_verify`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`capability_verify` lists each problem. Bundle problems name the file to change; a failed check means the tool or the check is wrong: reproduce it, then decide which. Fix with `capability_update`, never by loosening the check to pass.

## Self-heal
- First read `known_lessons` in the failed result, or call `lesson_search` with the error text: one may already be the fix.
- Tool failed: `capability_get`, reproduce it with a new check, `capability_update`, ask the user, `capability_apply`, re-run the stage.
- `load error` in `capability_list`: the last working tools still serve; fix the error the same way.
- An update made it worse: `capability_rollback`. No quick fix: `capability_disable` and tell the user.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A mistake in how capabilities are built becomes a lesson here with `lesson_add`, citing the failed proposal or check.
