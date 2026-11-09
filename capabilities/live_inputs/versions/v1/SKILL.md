---
name: live_inputs
description: Detect and write the inputs of an app's live runs - what a scientist tunes when starting one (input data, modes, seeds, sizes) - in the annotation phase; the Live Run view shows them as dropdowns. Load when exploring or annotating an app, before package, and when starting a live run.
---

Cross-references: [[app_explore]] [[prov_annotate]] [[moirai_run]] [[repro_package]]

# Live-run inputs

A live run is started from the Live Run view by a domain scientist who picks values, not by typing a command. Those
choices only exist once the app is understood, so the annotation phase writes them, on the run pipeline.

## Rules
- In the annotation phase (after explore and annotate, at the latest before the package gate): `live_inputs_detect`, then curate, then `live_inputs_write`, then `live_inputs_validate`. Show the user the inputs and the example command at the package gate.
- Curate, do not copy: keep what a scientist changes between long runs (input files, datasets, modes, seeds, sample counts, sizes, nodes, queue, time limit). Drop internals, paths of the tool chain, debug flags.
- Every input gets a plain label and a one-line help. A closed set is a `choice` with every option (files: the real files, relative to the app). Give defaults that reproduce the baseline.
- The command template is the real run: the baseline's command with `{name}` slots. Every slot is an input, every input is used.
- Starting a live run: `live_inputs_render(pipeline, values)` with what the user chose, show the command, then `moirai_live_run(command=...)`.

## Constraints
- Never invent options: options come from the code (argparse choices), the app's files, or the user.
- A value is shell-quoted into its slot; do not put shell syntax in values.

## Corner cases
- An app driven by a config file: the file is the input (a choice of the app's config files); fields inside it are inputs only if the run script takes them as flags or env.
- A batch app (flux batch run.sh): nodes, queue and time limit are inputs of the batch line.

## Corrector
Validator `live_inputs_validate`.

| Failed check | Correct |
|---|---|
| `written` | detect, curate, write |
| `consistent` | the problems name the slot or input: fix the template or the input list |
| `defaults_render` | give every required input a valid default |

## Self-heal
- `live_inputs_detect` finds nothing: the app is not Python or takes a config file; read its entry point and run script, and write the inputs by hand.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- An input a scientist asked for that was missing: add it and record a lesson for the app's workload capability.
