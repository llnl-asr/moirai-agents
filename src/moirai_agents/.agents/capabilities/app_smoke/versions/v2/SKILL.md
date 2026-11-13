---
name: app_smoke
description: Build and smoke-test the original application, unchanged. Load for the smoke stage of a run pipeline, for a smoke job that exited non-zero, for 'smoke passed, wrote 0 file(s)', for uv venv or pip failing to create a file under python3.13/site-packages on a compute node (Lustre, vast1, /usr/workspace), and before building an environment on the login node.
---

Cross-references: [[app_explore]] [[moirai_run]] [[pipeline_control]] [[capability_builder]] [[dftracer-build-app]] [[flux-alloc]] [[system-tuolumne]]

# Smoke test

## Rules
- A pipeline with a session uses `smoke_session`, with `build=false` for a plain script, and the session's build skills (`dftracer-build-app`, `software-cmake`, ...). `smoke_run` is for a pipeline with no session.
- Run the app exactly as the user does, before any change. A failure here is not ours.
- A stage passes only on exit code 0 of every step. The summary never says "smoke passed" otherwise; `smoke_validate` fails on any non-zero rc and shows the last log lines.
- Big builds go to compute nodes unless the user says so. `smoke_run` takes `build_where='compute'` (default; the build goes through `launcher`). `build_where='login'` is accepted only with `user_approved_login_build`, the user's own words, which is written in the job log header (`# build_where=login`, `# user_approved_login_build=...`) and the stage outputs. The build then runs bare on the login node inside the background job; the smoke run itself still goes through `launcher` on a compute node. Check a placement with `smoke_build_plan` first.
- Show at the gate: the command, the runtime, what it wrote. Ask whether that is the expected result: exit 0 with the wrong output is not a working app.
- The files listed in `outputs_created` are the first candidates for entities.

## Constraints
- Run single-process and small. It must finish in minutes.
- Use a launcher only when the login node cannot run it, and tell the user which. A build needs a launcher unless the login exception was granted.
- Never edit the source tree to make it pass. If a code change is needed, ask.
- Never paraphrase an approval: pass what the user wrote. If they did not say it, ask with `ask_user`.

## Site rule: creating files under python3.N/site-packages on a compute node
- Observed (main thread probes, tuolumne, 2026-10-30; reading is allowed): on compute nodes, creating a file under any `python3.N/site-packages` fails with ENOENT on Lustre, vast1 and /usr/workspace, while creating files in `lib/site-packages`, `lib/python3.13/other` and any write from a login node work. The cause is unknown: do not assert one.
- Evidence: job smoke-933998287 (pipeline elmerfold-prov-20261030-125944, tuolumne1009): `uv venv --no-managed-python --python=3.13 --relocatable /p/lustre5/haridev/elmerfold/openfoldenv` -> `failed to create file .../lib/python3.13/site-packages/_virtualenv.py: No such file or directory (os error 2)`, `flux-job: task(s) exited with exit code 2`. Lesson L-d4e3d3ef (app_explore): from tuolumne1607 `touch` in site-packages and site-packages/pip failed ENOENT, but worked in the venv root, lib, lib/python3.13, the parent and a fresh mkdir'd directory; `lfs getdirstripe` layouts are identical; pip hit the same ENOENT in wheel.py save() while the C++ wheel compiled on the node.
- What to do: (1) compile wheels on the compute node (launcher), install into the venv from the login node; or (2) ask the user for the login-build exception and run `smoke_run(build_where='login', user_approved_login_build=<their words>, launcher=...)`. Never create the venv elsewhere silently or edit setup scripts to dodge it.

## Corner cases
- The app writes outside its directory: `outputs_created` misses that; ask where the data goes.
- A command that needs MPI to start: use `flux run -n1`.
- A build that writes into the source tree: use an out-of-tree build directory via `cwd`.
- A dftracer step that reports no rc: only its status is judged; check its log if the output looks wrong.
- `smoke_session` has no `build_where`; if its build step must run on the login node, ask and use `smoke_run`.

## Corrector
Predictor `smoke_session`, `smoke_run`, validator `smoke_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `session_steps` | read the failed step's rc and log tail; re-run `smoke_session` with the right `extra_cmake_flags`, `command`, `subfolder`; load the build system's `software-*` skill |
| `exit_0` | fix the command or build in the `smoke_run` arguments, never the source; a file-creation error under site-packages on a compute node: see the site rule |
| smoke_run error 'needs user_approved_login_build' | ask the user; pass their words |
| a step that always needs the same fix | `capability_update`, so the tool does it |

## Self-heal
- Failed: read `log_tail`, find the root cause, fix the arguments and re-run. Missing module or library: ask the user only for facts you cannot find.
- `lost` job: the process died with the shell's allocation; re-run under a real allocation.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A build fix for this app or site: `lesson_add` with the log line.
- A step you had to do by hand every time: `capability_update`.
- A summary that said success while the rc was non-zero is a defect of the tool: reproduce it with a check first (smoke_rc_verdict).
