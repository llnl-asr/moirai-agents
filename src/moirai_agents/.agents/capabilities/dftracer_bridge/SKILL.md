---
name: dftracer_bridge
description: Reach dftracer-agents' tools from a capability. Load when a step needs its sessions, system detection or annotation tooling and no capability wraps it.
---

# dftracer bridge

moirai-agents builds on dftracer-agents, installed editable: sessions, site detection, build tooling, skills, permission policy.

## Rules
- Reuse before writing: `df_status`, then `df_call`, or the `mcp__dftracer__*` tools directly.
- A dftracer tool used repeatedly in one stage belongs in that stage's capability.

## Constraints
- All work stays in the session under `workspaces/`; take paths from `session_get_run_paths`, never build them by hand.
- Do not start a second dftracer server with another session root for pipeline work.

## Corner cases
- `available` false: the capabilities fall back to their own tools; say so at the gate.
- A dftracer change is picked up on the next call; the first call after it takes seconds.

## Corrector
Predictor `df_call`, validator `df_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

`df_validate` lists what is missing. Missing tools: a renamed dftracer tool means updating the capability that calls it; a stale checkout means updating it. Not installed is inconclusive: run `scripts/setup_env.sh`.

## Self-heal
- A dftracer tool is wrong: fix it in the dftracer-agents checkout, on a branch, with a test. It refreshes by itself.
- Import error: `df_refresh`, and re-run `scripts/setup_env.sh` if a package is missing.

## Self-learn
- A dftracer behaviour moirai work depends on: `lesson_add` with the tool and output.
