---
name: prov_annotate
description: Add moirai provenance to an application - find data accesses, agree entity types and activities with the user, insert them into a copy, validate. Load for the annotate stage.
---

# Annotate

Provenance records what data exists and how it became what it is, not timing.

## Rules
- Start from the science: which files or structures are the outputs a person cares about? Trace each backward through the code to its inputs.
- `annotate_scan`, then read the functions it names. Use `graph` or Grep to locate; do not read whole trees.
- Entity type: a kind of data, with a role and a description a reader can understand. Entity: one instance, keyed by something stable (a path, a sequence id).
- Activity: a function that turns some entities into others. Its `used` and `generated` lists are the graph's edges.
- Present the spec to the user at the gate, with the diff from `annotate_apply`, before integrating.

## Constraints
- The source tree is read-only; `annotate_apply` always works on `<pipeline>/annotated`.
- Never annotate a hot loop per iteration: a tight function can run millions of times. Annotate the function that owns the unit of data.
- Do not edit annotated files by hand: the next apply discards it. Change the spec, or update this tool.
- `key_expr` must be stable across processes: the same entity must get the same key everywhere, because its id is a hash of type and key.
- No timing or performance data in entities.

## Corner cases
- Python: `key_expr` may use the function's parameters and `result`. A generator or async function needs a wrapper; ask before changing it.
- Python workers: `_moirai_prov.start()` runs once per process, so forked and spawned workers are covered.
- C: `key_expr` is a `const char*` expression evaluated at the top of the function; a value computed later needs a separate activity on a helper function.
- C++: templates and overloads: `annotate_apply` takes the first definition; check the diff.
- An entity in memory with no file: use `store: memory` and a logical key.

## Corrector
Predictor `annotate_scan`, `annotate_spec`, `annotate_apply`, validator `annotate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

The original tool is `annotate_apply`, driven by the spec: correct the spec, re-apply, validate. Never hand-edit annotated files.

| Failed check | Correct |
|---|---|
| `functions_found` | `Class.method` in Python, plain name in C; re-run `annotate_spec` |
| `parses`, `compiles` | fix the `key_expr` type or scope |
| `inserted` | re-run `annotate_apply` |
| `types_used` | relate the type or drop it |
| `has_output` | ask the user which entity is the scientific artifact |
| a code pattern generated wrongly | `capability_update` with a check of that pattern |

## Self-heal
- `functions_found` failed: use `Class.method` in Python, the plain name in C.
- `compiles` failed on a moirai line: the `key_expr` has the wrong type or names a variable not yet declared.
- Generated code wrong for a pattern: `capability_update` with a check for that pattern.

## Self-learn
- A choice of entity or key the user corrected: `pipeline_feedback(kind=remember)`.
- A code pattern the tool mishandled: `pipeline_feedback(kind=process)`, then update this capability.
