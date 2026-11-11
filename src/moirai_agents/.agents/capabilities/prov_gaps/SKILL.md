---
name: prov_gaps
description: Fix a provenance gap a query exposed - a missed input, output, variable or attribute of a function or entity, or a function or entity missed entirely because of how the code is structured - by exploring the app in the annotation phase, then learning it twice, generically in the provenance capability and for the workload. Load when the user opens a gap from the Query view, or asks why the provenance cannot answer something.
---

Cross-references: [[prov_annotate]] [[prov_query]] [[prov_discover]] [[capability_builder]]

# Provenance gaps

A gap is what a computed answer could not explain: `undeclared_entity` (referenced, never declared), `missing_producer` (nothing recorded
making an entity: a function was missed), `no_inputs` / `no_outputs` / `no_attributes` (what a function was run with, returned, or what an
entity is, is not recorded), `not_recorded` (something the question names is not in the provenance at all), `reported` (the answer's write-up says the traces do not show
something; its detail is that sentence). The fix belongs in the
annotation, and the lesson belongs in two capabilities, so the next app and the next run of this one are captured right.

## Rules
1. `gap_list` on the app: the gap, the question that exposed it, and the plan that ran. Asked to work on ALL gaps: `gap_collect` first (every
   query's gaps onto the app, one per kind and subject), then work them as ONE batch: group the open gaps by function / entity, fix each group's
   annotation in one pass, learn once per structure, ask ONCE before the single re-run, re-run every question that showed a gap, then
   `gap_close` each.
2. Explore the app's code where the subject lives (search the source tree for the function, the entity's file pattern, the parameter names). Find WHY it was missed and name the cause:
   - `missed_inputs` / `missed_variables`: the parameters are not arguments - read from `self` or a config object, built into a command line inside the function, taken from the environment or a file.
   - `missed_outputs`: the result is written, returned through a callback, or kept on the object, not returned.
   - `missed_attributes`: the entity's facts that answer questions (sequence count, resolution, score) are computed but not recorded.
   - `missed_function` / `special_structure`: the work runs where a decorator does not reach - a subprocess or shell command, a scheduler or task callback (`on_finish`), a dynamically dispatched method, generated or `exec`'d code, a Maestro/YAML step, a C extension.
   - `missed_entity`: a file or value that matters is never declared.
3. Fix the annotation: extend the spec (`capture: {inputs: {label: expression}}`, `attributes`, a new activity in the right mode - patch, subclass, statement, yaml_step - or a new entity type), then `annotate_spec`, `annotate_apply`, `annotate_validate`.
4. Learn it GENERICALLY: `capability_update` of prov_annotate (or prov_discover for what discovery should have found) - a rule for this code structure that holds in any app, and a check that reproduces the miss. Never write app names, paths or user names there.
5. Learn it for the WORKLOAD: `capability_update` of `workload_<app>` (create it with `capability_new` if missing): the function, the exact capture and attribute expressions, and the structure, so this app is always annotated this way.
6. Ask the user before re-running the smoke or the baseline. After it ran, `evidence_rerun` (or `evidence_answer` with the same plan) on the new traces: the gap must be gone.
7. `gap_close` with the cause, both capability versions, how, and the re-check answer (without it the gap is fixed_pending_run). Then `gap_validate`.

## Constraints
- Do not close a gap by changing the question or the plan: the provenance must answer it.
- The generic rule describes a code structure, not this app; the workload capability holds what is specific.
- Capturing must never change the application's behaviour or fail it.

## Corner cases
- A value computed only inside a subprocess (a tool's own options): capture the command line; its flags are parsed into `flags`.
- A parameter that is a large object: name the fields that matter in `capture.inputs`, not the object.
- The gap is real in the code (the app never keeps the value): say so; close it with cause `other` and what the app would need to change.

## Corrector
Validator `gap_validate`.

| Failed check | Correct |
|---|---|
| `<gap>_learned` | install the generic and the workload update it names (capability_update, then the user's approval), then gap_close again |
| `<gap>_rechecked` | re-run the question after re-annotating and re-running, then gap_close with the answer |

## Self-heal
- `gap_close` returns problems: each names what to give or fix.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- Every closed gap is two lessons by construction: the generic rule and the workload entry. A gap kind that keeps coming back for one structure: make the rule a check in prov_annotate's annotate_validate.
