---
name: prov_annotate
description: Add moirai provenance to an application - find data accesses, agree entity types and activities with the user, insert them into a copy, validate. Load for the annotate stage.
---

# Annotate

Provenance records what data exists and how it became what it is, not timing. The discover stage and the user decide what to capture; here the spec becomes code.

- **Entity type**: a class of data. It has a role: `input` (comes from outside), `output` (the deliverables: every scientific artifact is one), `intermediate`, `parameter` or `reference`; and a description a reader can understand.
- **Entity**: one instance, keyed by something stable. Its id is a hash of type and key, the same in every language, so a C writer and a Python reader of one file join.
- **Activity**: spans a transformation; its `used` and `generated` are the graph's edges.

## Rules
- The spec is the contract: `annotate_spec` with `scientific_artifacts`, the types with `id_scheme` and description, and the activities. If the code disagrees with it, stop and report; never invent entity types.
- Immutable instances: data updated in place (a mesh each step, a model each epoch) gets a NEW key (`step=11`, `epoch=4`), so the graph stays a DAG.
- Keys are stable and meaningful: paths relative to the run dir, step numbers, sample keys, content hashes. Never pointers, never timestamps alone.
- Cross-process edges work by giving both sides the SAME type and key: sender `generated("halo", "step=5/r2->r3")`, receiver `used` with the same.
- Use the relation that says what happened: deletes are `invalidated`, in-place changes `updated`; containment and versioning are entity relations (`contains`, `revision_of`), not extra activities.
- Present the spec and the diff from `annotate_apply` at the gate.

## Constraints
- The application source is read-only; `annotate_apply` always works on the provenance copy, `<session>/provenance/source`.
- Never annotate a hot loop per iteration: a tight function can run millions of times. Annotate the function that owns the unit of data.
- Do not edit annotated files by hand: the next apply discards it. Change the spec, or update this tool.
- Do not pass hand-written descriptions in code: they live in the spec.
- `key_expr` must be stable across processes: the same entity must get the same key everywhere, because its id is a hash of type and key.
- No timing or performance data in entities.

## Corner cases
- Python: `key_expr` may use the function's parameters and `result`. A generator or async function needs a wrapper; ask before changing it.
- Python workers: `_moirai_prov.start()` runs once per process, so forked and spawned workers are covered.
- C: `key_expr` is a `const char*` expression evaluated at the top of the function; a value computed later needs a separate activity on a helper function.
- C++: templates and overloads: `annotate_apply` takes the first definition; check the diff.
- An entity in memory with no file: use `store: memory` and a logical key.
- First declaration of an entity in a process wins: its store and uri come from the first call, and later calls only add edges. Declare with the full store and uri the first time.
- Pickled pool tasks: the decorator keeps the function's name, so tasks pickled by name keep working.
- Python pools and spawned servers skip `atexit`; the generated helper also registers `multiprocessing.util.Finalize`.
- RPC edges without editing the framework: subclass the task type to record the driver side, and record the server side in the handler, with the identical type and key.

## Corrector
Predictor `discover_scan`, `annotate_spec`, `annotate_apply`, validator `annotate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

The original tool is `annotate_apply`, driven by the spec: correct the spec, re-apply, validate. Never hand-edit annotated files.

| Failed check | Correct |
|---|---|
| `functions_found` | `Class.method` in Python, plain name in C; re-run `annotate_spec` |
| `parses`, `compiles` | fix the `key_expr` type or scope |
| `inserted` | re-run `annotate_apply` |
| `types_used` | relate the type or drop it |
| a code pattern generated wrongly | `capability_update` with a check of that pattern |

## Self-heal
- `functions_found` failed: use `Class.method` in Python, the plain name in C.
- `compiles` failed on a moirai line: the `key_expr` has the wrong type or names a variable not yet declared.
- Generated code wrong for a pattern: `capability_update` with a check for that pattern.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A choice of entity or key the user corrected: `pipeline_feedback(kind=remember)`.
- A code pattern the tool mishandled: `pipeline_feedback(kind=process)`, then update this capability.
