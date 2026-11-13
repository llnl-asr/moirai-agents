---
name: prov_annotate
description: Add moirai provenance to an application - find data accesses, agree entity types and activities with the user, insert them into a copy (decorate a function, patch or subclass a method, annotate a statement, record a shell step of a Maestro YAML, invalidate, conditional edges), validate, and show the user an entity-type relationship diagram of what was annotated at the gate. Load for the annotate stage, or when a spec needs an inherited method, an inline subprocess step, a delete, a key from a local variable or an optional input, when the work is a shell command in a Maestro YAML `cmd:` step (AlphaFold 3, Rosetta FlexPepDock, BioTox, yaml_step), when a question about the parameters or flags of a step finds none recorded (a no-argument on_finish/run/__call__ hook whose command line was built in the constructor), or when the annotate verdict is inconclusive about PYTHONPATH, moirai_env.sh or maestro_dry.
---
Cross-references: [[moirai_integrate]] [[workload_elmerfold]] [[workload_biotox]] [[dftracer-provenance]] [[dftracer-annotate-general]]

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
- At the gate show the user three things: the diagram from `annotate_diagram` (see Gate), the spec, and the diff from `annotate_apply`.
- Choose the mode by where the code is:
  - `wrap` (default): a function or method defined in the file. `function: "Class.method"`.
  - `patch`: a method that exists but you cannot or should not decorate in place, or one inherited (`ServerTask.start`): `target: "ServerTask.start"`. A line `ServerTask.start = _moirai_prov.activity(...)(ServerTask.start)` is placed before the `if __name__ == "__main__"` guard, or at the end. `params` is needed when the target is not defined in that file.
  - `subclass`: insert `class ProvX(Base)` whose method calls `super()`. Fields `base`, `method`, optional `class_name`, `rebind` (also `Base = ProvX` in that module), `params`. The driver->server hop of Elmerfold: `generated inference_request` in the driver's `ServerTask` subclass `start()`, `used` with the same key in the server's `run_inference`.
  - `statement`: a one-shot activity (no duration) placed before or after one statement: `anchor` (text found in a statement's own header or line), `at` (before|after, default after), `occurrence` (default 1), `in` (a function to search in). Its key expressions read the surrounding locals by closure. Use for an inline `SubprocessTask`/`subprocess.run` step, and for ids from `args.incsv`, `args.config_preset`, `seed`, `config`. If the activity must span a function, wrap the function with keys from its parameters, and add a statement activity for what is computed inside it.
  - `yaml_step`: a shell step of a Maestro YAML (`cmd: |`), where the work is a command (AlphaFold 3 `run_alphafold.py`, Rosetta `FlexPepDocking`) and no Python or C function wraps it. Fields `file` (the YAML), `step`, `name`, `used`/`generated`, optional `params` (Maestro names a parameter generator makes and the YAML never writes), `shell_vars`, `record_on_failure`. See "Maestro shell steps".
- Conditional edge: `when` on an entity (`"when": "os.path.exists(path)"`) or on a relation; the edge is recorded only if it is true. For `allow_failure` outputs (templatefeat, model_1_ptm).
- Entity edge: `relations: [{"relation": "contains", "subject": {type, key_expr, store}, "object": {...}, "when": "..."}]`, recorded after the function returns (so `result` can be used).

## Maestro shell steps
- What is written: a block at the top of the step's `cmd` in the COPY of the YAML (marked `# >>> moirai-agents yaml_step <step>` ... `# <<<`). It installs an EXIT trap. When the step ends the trap calls `_moirai_prov.py yaml-step` as a script with the rows whose files exist, in a subshell with all output discarded. It never calls `exit`, so the step's exit status and its early `exit 0` / `exit 1` are unchanged, and it does nothing when moirai is down or not found.
- Which Python: `$MOIRAI_PYTHON`, else `dirname($MOIRAI_SERVICE)/python`, else the `python` beside `moirai-service` on PATH, else the moirai Python this machine had when annotate ran. This is resolved BEFORE the step activates its own venv, so AF3's venv and biotox-env (python 3.12) do not matter: the helper runs under the moirai venv (3.13) with `PYTHONPATH`, `PYTHONHOME` and `LD_PRELOAD` unset. The helper is the single root `_moirai_prov.py`, found through `PYTHONPATH` (as integrate exports it) or `$(SPECROOT)/<path to the root>`.
- Keys and uris are shell text written inside double quotes after Maestro has substituted its variables: `$(SEQ)`, `$(SEED)`, `$(SAMPLE)`, `$(PEP_ID)`, `$(TARGET)`, `$(<step>.workspace)`, and shell variables the cmd assigns (`${CIF_FILE}`). Give both sides of a hop the same type and key text, for BioTox the case id `$(TARGET)/$(PEP_ID)/seed-$(SEED)_sample-$(SAMPLE)`; only the `uri` (the file) differs per side.
- Existence: an entity on `local_disk`, `parallel_fs` or `burst_buffer` is recorded only if its `uri` exists. To tie a record to what the step really did, use a shell variable the cmd sets (`"uri": "${PDB_FILE}"`): an early exit leaves it unset and nothing is recorded. `when` is the argument list of `[ ... ]` (`-f "$CIF_FILE"`) and replaces the default test; `exists: false` records always. A file store without uri, when or exists:false is refused.
- A step that exits non-zero records nothing unless `record_on_failure: true`.
- The activity is written when the step ends, so it has no duration; a step is one activity per Maestro instance.
- Refused with a reason: a step with its own `trap ... EXIT`, one that uses `exec`, a `shell:` other than bash or sh, a `cmd` that is not a `|` block, an entity text with a double quote, backtick or `$(command)`, and `invalidated`, `updated`, `relations` (Python only).
- A scientific artifact may be generated only by `yaml_step` activities; `annotate_spec` counts them.
- Try it before annotate_spec: `annotate_preview` with the YAML as text and `run_steps` runs the wrapped steps with bash against a fake moirai (`variables` for the Maestro names, `step_env` to break moirai on purpose).

## Capture the context (inputs, outputs, attributes)
- Every activity records what it was run with and what it returned, by default, one small entity per value with uri `name=value`: `<activity>.input` (role parameter, used) for its arguments with their defaults, the attributes of the object a method runs on (`self`), and the flags of every command line among them (flags first); `<activity>.output` (generated) for its return value. A Maestro step records the `$(VAR)` study variables its command uses. One entity per value because a trace keeps about 250 characters of a uri and rewrites quotes and brackets: never put JSON in a uri.
- Where the parameters are not in the arguments - a method that reads `self.config`, a command built inside the function, a value read from a file or the environment - name them: `capture: {inputs: {label: expression}}` (expressions of the arguments; for a statement, of its locals; for a Maestro step, shell text such as `${PEP_NSTRUCT}`).
- No-argument hook whose command or parameters were built in the constructor (structure: a callback/hook method such as `on_finish`, `on_done`, `run`, `__call__` taking only `self`, of an object whose `__init__` or base class built the command line or parameter set and stored it on `self`): its arguments are empty, so default capture has nothing to record and a question about its flags finds no input entity (missed_inputs / special_structure). Name the stored field: `capture: {inputs: {"command": "self.run_str"}}` (use the real attribute; one label per field). The flags are then recorded as `<activity>.input` entities, flags first. Explore for it: for each wrapped method with no arguments besides `self`, read the class constructor and its base classes (`__init__`, `super().__init__`, a `build_*`/`make_*` command helper) for the attribute that holds the command string, argv list or parameter dict (look for assignments to `self.<x>` of a joined command, `shlex.join`, `" ".join`, or `subprocess` arguments), and check that the value is final when the hook runs. Also look at the other wrapped hooks of the same class family: they share the base class field.
- Check that a hook is captured: `annotate_preview` with a two-class file (a base class whose `__init__` stores `self.run_str = "tool --qid 0.2 --diff 3000"`, a subclass with `def on_finish(self): pass`, a driver that instantiates and calls it), the spec wrapping `Sub.on_finish` first WITHOUT `capture` (the fake moirai shows no input entity carrying `--qid`/`--diff`), then WITH `capture: {inputs: {"command": "self.run_str"}}` (the input entity carries both flags). Add the pair as a check when this rule is changed.
- Entities may carry attributes worth asking about (sequence count, resolution, score): `attributes: {label: expression}` on the used or generated entry; they become `<type>.attribute` entities (`name=value`), part_of the entity.
- `capture: false` only for a hot activity where the cost matters; say why in the spec.
- Every entity type's description is extended with its attributes and the activities expected to produce and use it: write the description itself as what the entity is.

## Inputs, outputs and attributes (captured by default)
- Every Python activity (wrap, patch, subclass) records, per call: its arguments with their defaults, the attributes of the object it runs on (`self`, two levels), the flags of any command line among them (`--qid 0.2`), as `<activity>.input` entities (`name=value`, role parameter, used); and its return value as `<activity>.output` entities (generated). Nothing to write in the spec; `capture: false` turns it off for a hot function.
- Parameters that live elsewhere - a config object, an environment variable, a command string built in another function - are named in the spec: `capture: {inputs: {"filter command": "self.run_str", "threshold": "cfg.qid"}}` (expressions over the call's arguments; for a statement, over its locals).
- A Maestro step (`yaml_step`) records the `$(VAR)` study parameters its command uses; `capture: {inputs: {NAME: "$VAR"}}` adds shell variables.
- An entity's attributes: `attributes: {"n_sequences": "len(result)", "size": "os.path.getsize(path)"}` on the entity in an activity; they become `<type>.attribute` entities `part_of` it. Give each entity type a description; the generated description adds its attributes and the activities that produce and use it.
- When exploring an app, find each step's real knobs (thresholds, filters, models, seeds) and make sure they are captured: arguments are automatic, anything else goes in `capture.inputs`. Ask the user which parameters matter when it is not clear.
- Values are cut at ~240 characters, and the trace replaces quotes and brackets: record a long or structured value by its parts (named inputs), not as one string.

## Constraints
- The application source is read-only; `annotate_apply` always works on the provenance copy, `<session>/provenance/source`.
- Never annotate a hot loop per iteration: a tight function can run millions of times. Annotate the function that owns the unit of data. A `statement` activity inside a loop runs per iteration.
- Do not edit annotated files by hand: the next apply discards it. Change the spec, or update this tool.
- Do not pass hand-written descriptions in code: they live in the spec.
- `key_expr` must be stable across processes: the same entity must get the same key everywhere, because its id is a hash of type and key.
- No timing or performance data in entities.
- `patch`, `subclass`, `statement`, `relations` are Python only. C and C++ take `wrap`, plus `invalidated`, `updated` and `when`; in C a `key_expr` and a `when` are evaluated at the top of the function, over its parameters.
- The framework class must be in the copied tree. A class in an installed package is not changed; say so at the gate and annotate the driver side.
- Never type a count, a type or an edge into the gate message: the diagram, its counts and its notes come from `annotate_diagram`, which reads the applied spec.

## Gate
After `annotate_apply` and a passing `annotate_validate`:
1. Call `annotate_diagram(pipeline)`. It writes `<session>/provenance/annotation_diagram.md` and returns `markdown` (counts, Mermaid diagram, legend, what was collapsed, one line per activity, and the one-line "what to decide").
2. Call `ask_user(pipeline, stage="annotate", ...)` with the returned `markdown` in the `context`, followed by the spec summary and the diff. Do not retype the diagram. Ask the user to approve the annotation or say what to change.
3. Say in the question the one-line guidance the tool returns: is every dataset you care about a box, is each made or read by the activity you expect, are the deletes (red dotted) and optional edges (dashed, `when`) right. A missing type, a wrong edge or an activity on the wrong function is process feedback naming it: change the spec, `annotate_apply`, validate, draw again.
4. The web UI shows the `context` as plain text, not as a rendered diagram (it escapes it). The Mermaid block is therefore readable as text; the outline lines under it carry the same facts, and the file `annotation_diagram.md` can be opened in any Mermaid viewer. Do not claim the user is looking at a picture.
- Size: a diagram draws every type and activity. Types of one family (model_1 .. model_5) become one node with a count; beyond `budget` edges (default 60) the labels of unconditional used/generated edges are dropped; the notes in the result say exactly what, and the counts stay the spec's.

## Corner cases
- Python: `key_expr` may use the function's parameters and `result` (not in `used`, and not in `statement`). A generator or async function needs a wrapper; ask before changing it (async targets were not tried).
- `key_expr` and `when` may use module-level names of that file (imports included). Names that are in scope nowhere there are reported by `annotate_validate` as `names_resolve`; the run would raise NameError.
- `statement`: names must be assigned before the anchor (`at: before`) or by it (`at: after`); a `return`/`raise`/`continue`/`break` anchor cannot be `after`, an `elif` header cannot be an anchor; an `after` on a compound statement (`with`, `for`) goes after the whole block.
- `_moirai_prov.py` is ONE file at the root of the annotated tree, not beside each module: modules do `import _moirai_prov` absolutely, so copies beside modules would be several modules with several start/stop states. It is found through `PYTHONPATH=<annotated root>`, which `integrate_apply` writes to `moirai_env.sh` and which spawned and forked workers inherit. `annotate_validate` checks one helper at the root. The PYTHONPATH rule belongs to the integrate stage, which comes after this one: until `moirai_env.sh` exists the verdict lists it under `deferred` (owner `integrate`) and it does not count; `integrate_validate` enforces it (`env_pythonpath`, `env_import`). If `moirai_env.sh` exists already (a re-run), `annotate_validate` checks it as well. `python -I` or `-P`/`PYTHONSAFEPATH` do not see it.
- Python workers: `_moirai_prov.start()` runs once per process, so forked and spawned workers are covered.
- C: `key_expr` is a `const char*` expression evaluated at the top of the function; a value computed later needs a separate activity on a helper function.
- C++: templates and overloads: `annotate_apply` takes the first definition; check the diff.
- An entity in memory with no file: use `store: memory` and a logical key.
- First declaration of an entity in a process wins: its store and uri come from the first call, and later calls only add edges. Declare with the full store and uri the first time.
- Pickled pool tasks: the decorator keeps the function's name, so tasks pickled by name keep working. A `patch` also keeps it; a rebound `subclass` does not (the class name changes), so pickled instances of the base should not cross processes.
- Python pools and spawned servers skip `atexit`; the generated helper also registers `multiprocessing.util.Finalize`.
- RPC edges without editing the framework: `subclass` or `patch` the task type to record the driver side, and record the server side in the handler, with the identical type and key.
- Try a spec first with `annotate_preview` (files as text, `run` a script): it applies the spec in a temp dir and runs the script against a fake moirai that records activities, relations and links, without a pipeline. `annotate_validate_preview` gives the verdict the same way; `annotate_diagram_preview` draws a draft spec.
- The `coverage` check stays inconclusive when the smoke stage listed no output files; that is an earlier stage and is fixed there.
- yaml_step: Maestro parameter-generator names (SEQ, SEED, SAMPLE, PEP_ID, TARGET) are known only through their use in the YAML; one the YAML never writes goes under the activity's `params`. A `$(<step>.workspace)` is allowed only for an ancestor in `depends`.
- yaml_step: `maestro_dry` needs the same arguments the user runs maestro with when the study needs a parameter generator (`maestro_args: "--pgen gen.py --pargs inputs:<path>"`). If maestro cannot dry-run the SOURCE either, the check is inconclusive and says so; it never blames the wrapper for a source that fails by itself.
- yaml_step: the YAML is edited line by line, never re-dumped, so comments and layout are kept; `yaml_intact` proves the YAML minus the blocks is the source byte for byte.
- yaml_step: MOIRAI_YAML_STEP_LOG, if set in the step's environment, receives the helper's stderr (otherwise /dev/null): the way to see why nothing was recorded.

## Corrector
Predictor `discover_scan`, `annotate_spec`, `annotate_apply`, `annotate_preview`, `annotate_diagram`, validator `annotate_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

The original tool is `annotate_apply`, driven by the spec: correct the spec, re-apply, validate. Never hand-edit annotated files.

| Failed check | Correct |
|---|---|
| `functions_found` | `Class.method` in Python, plain name in C; for a missing anchor or a name that is not in the module, fix `anchor`/`in`/`target`/`params`; for yaml_step fix `step` or the text the message names (quote, `exec`, own EXIT trap); re-run `annotate_spec` |
| `parses`, `compiles` | fix the `key_expr` type or scope |
| `names_resolve` | the expression uses a name not in scope there: use a parameter, `result`, a module name, or move the anchor; for yaml_step use a Maestro variable the study defines or uses, an ancestor's `$(step.workspace)`, a shell variable the cmd assigns, or list it under `params` / `shell_vars` |
| `helper_single` | exactly one `_moirai_prov.py` at the annotated root; re-run `annotate_apply` |
| `helper_on_pythonpath` (only when `moirai_env.sh` exists) | `integrate_apply` must export `PYTHONPATH=<annotated root>`; re-run it. If it does not exist yet, this is not a failure: it is under `deferred` and `integrate_validate` enforces it |
| `inserted` | re-run `annotate_apply` |
| `types_used` | relate the type or drop it |
| `yaml_intact` | the annotated YAML was edited or the source changed: re-run `annotate_apply` |
| `bash_syntax` | a key, uri or when breaks the shell: change that text in the spec, re-run `annotate_apply` |
| `maestro_dry` fails | the wrapper broke the YAML (it passes on the source): re-run `annotate_apply`; if it persists `capability_update` with a check. Inconclusive: pass `maestro=` and `maestro_args=` |
| a code pattern generated wrongly | `capability_update` with a check of that pattern (use `annotate_preview`) |
| a gap says an activity has no parameters/flags recorded (no `.input` entity with the flags) | the wrapped method takes no arguments and its command was built elsewhere: add `capture.inputs` naming the field on `self` (see Capture the context), `annotate_apply`, validate |
| the diagram is wrong or unreadable | it is drawn from the applied spec: fix the spec and re-apply; for a layout or size problem `capability_update` with a check using `annotate_diagram_preview` |

## Self-heal
- `functions_found` failed: use `Class.method` in Python, the plain name in C.
- `compiles` failed on a moirai line: the `key_expr` has the wrong type or names a variable not yet declared.
- Generated code wrong for a pattern: `capability_update` with an `annotate_preview` check for that pattern.
- A verdict inconclusive for a check that a later stage owns: that is a tool-design fault, not something to wait out. Move the check to the later stage's validator and list it under `deferred` here (as `helper_on_pythonpath` was).
- A yaml_step records nothing at run time: set `MOIRAI_YAML_STEP_LOG=<file>` for the step and read it; check `MOIRAI_PYTHON` / `MOIRAI_SERVICE` reach the step's environment (flux and maestro pass only what the launcher forwards).
- A parameters question finds no input entity for an annotated step: check whether the wrapped method has arguments; if only `self`, find the constructor field that holds the command and add it to `capture.inputs`. The fix is verified by `annotate_validate` plus the next traced run; until a run, the gap stays pending.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A choice of entity or key the user corrected: `pipeline_feedback(kind=remember)`.
- A code pattern the tool mishandled: `pipeline_feedback(kind=process)`, then update this capability.
- What the user said they need to decide at the gate (for example, the diagram of what was covered): `pipeline_feedback(kind=process)`, then update this capability with a check.
- A gap closed by a missing `capture` on a no-argument hook (G-082a03t0): when more such structures are met (parameters kept in a config file, environment or a sibling object), extend the capture rule with that structure.
