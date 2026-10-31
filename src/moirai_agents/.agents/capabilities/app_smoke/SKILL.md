---
name: app_smoke
description: Build and smoke-test the original application, unchanged. Load for the smoke stage of a run pipeline.
---

# Smoke test

## Rules
- A pipeline with a session uses `smoke_session`, with `build=false` for a plain script, and the session's build skills (`dftracer-build-app`, `software-cmake`, ...). `smoke_run` is for a pipeline with no session.
- Run the app exactly as the user does, before any change. A failure here is not ours.
- Show at the gate: the command, the runtime, what it wrote. Ask whether that is the expected result: exit 0 with the wrong output is not a working app.
- The files listed in `outputs_created` are the first candidates for entities.

## Constraints
- Run single-process and small. It must finish in minutes.
- Use a launcher only when the login node cannot run it, and tell the user which.
- Never edit the source tree to make it pass. If a code change is needed, ask.

## Corner cases
- The app writes outside its directory: `outputs_created` misses that; ask where the data goes.
- A command that needs MPI to start: use `flux run -n1`.
- A build that writes into the source tree: use an out-of-tree build directory via `cwd`.

## Corrector
Predictor `smoke_session`, `smoke_run`, validator `smoke_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `session_steps` | read the failed step's error; re-run `smoke_session` with the right `extra_cmake_flags`, `command`, `subfolder`; load the build system's `software-*` skill |
| `exit_0` | fix the command or build in the `smoke_run` arguments, never the source |
| a step that always needs the same fix | `capability_update`, so the tool does it |

## Self-heal
- Failed: read `log_tail`, find the root cause, fix the arguments and re-run. Missing module or library: ask the user only for facts you cannot find.
- `lost` job: the process died with the shell's allocation; re-run under a real allocation.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A build fix for this app or site: `lesson_add` with the log line.
- A step you had to do by hand every time: `capability_update`.
