---
name: prov_annotate
description: Add moirai provenance to an application - find data accesses, agree entity types and activities with the user, insert them into a copy (decorate a function, patch or subclass a method, annotate a statement, invalidate, conditional edges), validate. Load for the annotate stage, or when a spec needs an inherited method, an inline subprocess step, a delete, a key from a local variable or an optional input.
---
Cross-references: [[moirai_integrate]] [[workload_elmerfold]] [[dftracer-provenance]] [[dftracer-annotate-general]]

# Annotate

Provenance records what data exists and how it became what it is, not timing. The discover stage and the user decide what to capture; here the spec becomes code.

- **Entity type**: a class of data. It has a role: `input` (comes from outside), `output` (the deliverables: every scientific artifact is one), `intermediate`, `parameter` or `reference`; and a description a reader can understand.
- **Entity**: one instance, keyed by something stable. Its id is a hash of type and key, the same in every language, so a C writer and a Python reader of one file join.
- **Activity**: spans a transformation; its `used`, `generated`, `invalidated` and `updated` lists are the graph's edges.
- **Entity relation**: an edge between two entities with no activity: `contains`, `derived_from`, `revision_of`, `part_of`, `specialization_of`, `alternate_of`, `depends_on`.

moirai itself has all of these (moirai/include/moirai/provenance.h): activity relations USED, GENERATED, INVALIDATED, UPDATED and the seven entity relations above. A delete is therefore `invalidated`; nothing is invented.

## Rules
- The spec is the contract: `annotate_spec` with `scientific_artifacts`, the types with `id_scheme` and description, and the activities. If the code disagrees with it, stop and report; never invent entity types.
- Immutable instances: data updated in place (a mesh each step, a model each epoch) gets a NEW key (`step=11`, `epoch=4`), so the graph stays a DAG.
- Keys are stable and meaningful: paths relative to the run dir, step numbers, sample keys, content hashes. Never pointers, never timestamps alone.
- Cross-process edges work by giving both sides the SAME type and key: sender `generated("halo", "step=5/r2->r3")`, receiver `used` with the same.
- Use the relation that says what happened: deletes are `invalidated` (a `cleanup` or `delete_x` function lists the entity under `invalidated`), in-place changes `updated`; containment and versioning are `relations`, not extra activities.
- Present the spec and the diff from `annotate_apply` at the gate.
- Choose the mode by where the code is:
  - `wrap` (default): a function or method defined in the file. `function: "Class.method"`.
  - `patch`: a method that exists but you cannot or should not decorate in place, or one inherited (`ServerTask.start`): `target: "ServerTask.start"`. A line `ServerTask.start = _moirai_prov.activity(...)(ServerTask.start)` is placed before the `if __name__ == "__main__"` guard, or at the end. `params` is needed when the target is not defined in that file.
  - `subclass`: insert `class ProvX(Base)` whose method calls `super()`. Fields `base`, `method`, optional `class_name`, `rebind` (also `Base = ProvX` in that module), `params`. The driver->server hop of Elmerfold: `generated inference_request` in the driver's `ServerTask` subclass `start()`, `used` with the same key in the server's `run_inference`.
  - `statement`: a one-shot activity (no duration) placed before or after one statement: `anchor` (text found in a statement's own header or line), `at` (before|after, default after), `occurrence` (default 1), `in` (a function to search in). Its key expressions read the surrounding locals by closure. Use for an inline `SubprocessTask`/`subprocess.run` step, and for ids from `args.incsv`, `args.config_preset`, `seed`, `config`. If the activity must span a function, wrap the function with keys from its parameters, and add a statement activity for what is computed inside it.
- Conditional edge: `when` on an entity (`"when": "os.path.exists(path)"`) or on a relation; the edge is recorded only if it is true. For `allow_failure` outputs (templatefeat, model_1_ptm).
- Entity edge: `relations: [{"relation": "contains", "subject": {type, key_expr, store}, "object": {...}, "when": "..."}]`, recorded after the function returns (so `result` can be used).

## Constraints
- The application source is read-only; `annotate_apply` always works on the provenance copy, `<session>/provenance/source`.
- Never annotate a hot loop per iteration: a tight function can run millions of times. Annotate the function that owns the unit of data. A `statement` activity inside a loop runs per iteration.
- Do not edit annotated files by hand: the next apply discards it. Change the spec, or update this tool.
- Do not pass hand-written descriptions in code: they live in the spec.
- `key_expr` must be stable across processes: the same entity must get the same key everywhere, because its id is a hash of type and key.
- No timing or performance data in entities.
- `patch`, `subclass`, `statement`, `relations` are Python only. C and C++ take `wrap`, plus `invalidated`, `updated` and `when`; in C a `key_expr` and a `when` are evaluated at the top of the function, over its parameters.
- The framework class must be in the copied tree. A class in an installed package is not changed; say so at the gate and annotate the driver side.

## Corner cases
- Python: `key_expr` may use the function's parameters and `result` (not in `used`, and not in `statement`). A generator or async function needs a wrapper; ask before changing it (async targets were not tried).
- `key_expr` and `when` may use module-level names of that file (imports included). Names that are in scope nowhere there are reported by `annotate_validate` as `names_resolve`; the run would raise NameError.
- `statement`: names must be assigned before the anchor (`at: before`) or by it (`at: after`); a `return`/`raise`/`continue`/`break` anchor cannot be `after`, an `elif` header cannot be an anchor; an `after` on a compound statement (`with`, `for`) goes after the whole block.
- `_moirai_prov.py` is ONE file at the root of the annotated tree, not beside each module: modules do `import _moirai_prov` absolutely, so copies beside modules would be several modules with several start/stop states. It is found through `PYTHONPATH=<annotated root>`, which `integrate_apply` writes to `moirai_env.sh` and which spawned and forked workers inherit. `annotate_validate` checks one helper at the root and, once integrate has run, that `moirai_env.sh` names the root. `python -I` or `-P`/`PYTHONSAFEPATH` do not see it.
- Python workers: `_moirai_prov.start()` runs once per process, so forked and spawned workers are covered.
- C: `key_expr` is a `const char*` expression evaluated at the top of the function; a value computed later needs a separate activity on a helper function.
- C++: templates and overloads: `annotate_apply` takes the first definition; check the diff.
- An entity in memory with no file: use `store: memory` and a logical key.
- First declaration of an entity in a process wins: its store and uri come from the first call, and later calls only add edges. Declare with the full store and uri the first time.
- Pickled pool tasks: the decorator keeps the function's name, so tasks pickled by name keep working. A `patch` also keeps it; a rebound `subclass` does not (the class name changes), so pickled instances of the base should not cross processes.
- Python pools and spawned servers skip `atexit`; the generated helper also registers `multiprocessing.util.Finalize`.
- RPC edges without editing the framework: `subclass` or `patch` the task type to record the driver side, and record the server side in the handler, with the identical type and key.
- Try a spec first with `annotate_preview` (files as text, `run` a script): it applies the spec in a temp dir and runs the script against a fake moirai that records activities, relations and links, without a pipeline.

## Corrector
Predictor `discover_scan`, `annotate_spec`, `annotate_apply`, `annotate_preview`, validator `annotate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

The original tool is `annotate_apply`, driven by the spec: correct the spec, re-apply, validate. Never hand-edit annotated files.

| Failed check | Correct |
|---|---|
| `functions_found` | `Class.method` in Python, plain name in C; for a missing anchor or a name that is not in the module, fix `anchor`/`in`/`target`/`params`; re-run `annotate_spec` |
| `parses`, `compiles` | fix the `key_expr` type or scope |
| `names_resolve` | the expression uses a name not in scope there: use a parameter, `result`, a module name, or move the anchor |
| `helper_single` | exactly one `_moirai_prov.py` at the annotated root; re-run `annotate_apply` |
| `helper_on_pythonpath` | `integrate_apply` must export `PYTHONPATH=<annotated root>`; re-run it |
| `inserted` | re-run `annotate_apply` |
| `types_used` | relate the type or drop it |
| a code pattern generated wrongly | `capability_update` with a check of that pattern (use `annotate_preview`) |

## Self-heal
- `functions_found` failed: use `Class.method` in Python, the plain name in C.
- `compiles` failed on a moirai line: the `key_expr` has the wrong type or names a variable not yet declared.
- Generated code wrong for a pattern: `capability_update` with an `annotate_preview` check for that pattern.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A choice of entity or key the user corrected: `pipeline_feedback(kind=remember)`.
- A code pattern the tool mishandled: `pipeline_feedback(kind=process)`, then update this capability.
