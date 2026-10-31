---
name: repro_package
description: Generate final_provenance/, the folder that reproduces a run for someone else, and prove it in a clean shell. Load for the package stage.
---

# Reproduction package

## Rules
- `repro_build`, then `repro_verify`, then `repro_validate`. Verify, do not assert: a package counts only after it ran from a fresh copy in a clean login shell and reproduced the counts.
- `config.ini` is the only file with site values. Every script sources `lib_load_config.sh`, and the environment overrides the config.
- The instrumentation ships as a patch against the pinned app; the app itself is not committed, unless it has no URL and must be bundled.
- Pin everything: the app and the runtime, by full commit.
- Show the user the isolated run's counts at the gate.

## Constraints
- No username, home path, session path or job id in any script. Use `${USER}` and `${HOME}` in `config.ini`.
- `isolation_root` is outside the session and on a file system compute nodes can see.
- Do not edit the package by hand: `repro_build` starts over. Fix the tool or the pipeline's inputs.
- Do not make the check pass by loosening `inputs/expected.json`.

## Corner cases
- Use a full 40-character SHA: branches get merged and deleted, and GitHub and GitLab refuse a short-SHA fetch.
- Lmod's init and `module` are not `set -u` clean: wrap them with `set +u` in the loader.
- A non-login `env -i` shell has no MODULEPATH: verify from `bash -l`, which is what a new user gets.
- `pip uninstall dftracer` leaves its tree: remove `<site-packages>/dftracer` before reinstalling.
- A non-deterministic app gives different counts: tell the user; do not hide it.
- Use `reuse_venv` to verify quickly, and say at the gate that the install step was not exercised.
- Publishing to a shared group directory: `chmod g+rX` every parent you create.

## Corrector
Predictors `repro_build`, `repro_verify`, validator `repro_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `portable` | a value belongs in `config.ini`; change the pipeline's command or `output_root`, then `repro_build` |
| `pinned` | install from a release or a git commit, so a version can be named |
| `isolated_run` | the log names what the session gave and the package did not: usually a module, a path or an input |
| `reproduced` | same input? same scale? a non-deterministic app needs the user's call |

## Self-heal
- `moirai-service` fails only in the isolated run: the library path is set in `03_build.sh` and `04_run.sh`; compare with `moirai_env.sh`.
- `patch` fails: the app is not at the pinned state.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Each thing an isolated run exposed: `capability_update`, so the next package has it, and `lesson_add` for site facts.
