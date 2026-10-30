---
name: app_explore
description: Explore the system and the application before anything changes. Load for the explore stage of a run pipeline.
---

# Explore

## Rules
- `explore_run`, read the result, then look at the entry points and README to understand what data the app takes in and makes.
- Tell the user, at the gate: the system, how the app is built and started, the scientific data you believe it handles, and what you are unsure of.
- Confirm the small test command: it must finish in minutes on one node.

## Constraints
- Read-only: nothing in the source tree changes here.
- Do not guess the build. If there are several build files, ask which one is used.
- Login nodes are for exploring only; note what must go to a compute node.

## Corner cases
- A driver script that launches the real binary: the entry point to annotate is the program that touches the data, not the script.
- Mixed Python and C: both can be annotated; entity ids agree across languages.
- Fortran only: moirai has no Fortran API. Say so at the gate.
- No `/dev/shm`: moirai still writes spill files, and live monitoring works on them.

## Corrector
Predictor `explore_run`, validator `explore_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `moirai`, `dftracer_utils` | give the user the printed fix; re-run `explore_run` after |
| `language` | Fortran only has no moirai API: tell the user |
| `entry_point` | ask which file starts the app, then correct the tool's scan with `capability_update` if it should have found it |
| `dev_shm` | free it, or plan a smaller `MOIRAI_PC_BLOCKS` |

## Self-heal
- `moirai` check failed: give the user the `fix` it prints; `MOIRAI_AGENTS_MOIRAI_PYTHON` in `.env` is read at startup and by each new MCP server.
- Wrong language census: a vendored tree is being counted; update the tool's skip list with `capability_update`.

## Self-learn
- A system fact the next run needs (module to load, compiler that works): `lesson_add`.
- A class of app the tool misread: `capability_update` with a check for it.
