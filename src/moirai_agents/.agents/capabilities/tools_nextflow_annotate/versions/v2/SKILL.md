---
name: tools_nextflow_annotate
description: Annotate a COPY of a Nextflow (nf-core) pipeline such as taxteller so every process task reports its activity and entities to moirai - a bash EXIT trap at the top of each process script block calls a generated helper, idempotent marked blocks, Groovy escaping of $ and backslash, per-process entity plan, entity relations, diagram, moirai_overlay.config (env, podman bind mounts) and the job-script lines (dftracer with moirai, one moirai-service per node). Load for the annotate stage of a Nextflow pipeline, when a process script needs provenance, when a parse fails after annotation, when a process exit status or caching changed, or when an annotated task records nothing.
---
Cross-references: [[prov_annotate]] [[software_nextflow]] [[workload_taxteller]] [[prov_discover]] [[prov_diagram]] [[tools_moirai_venv]] [[moirai_integrate]]

# Annotating a Nextflow pipeline

prov_annotate covers Python, C/C++ and Maestro `yaml_step`; a Nextflow process is none of these: its work is a bash script that Groovy renders per task. This capability does for a process `script:` block what prov_annotate's yaml_step does for a Maestro `cmd:`: a marked block at the top of the script installs an EXIT trap that, when the task ends, calls a generated helper which records one moirai activity with its entities.

Order of the tools (each a step, the next one reads the previous result):
1. `nextflow_annotate_spec`: per-process spec from the discovery record and the module scripts (saved as `nf_spec.json` in the provenance dir).
2. `nextflow_annotate_diagram`: nodes and edges for `diagram_publish_graph` (entity types, activities, databases, images, used/generated edges and relation edges). The user approves this at the gate.
3. `nextflow_annotate_preview`: one process hook run with bash flags `-C -e -u -o pipefail` against a fake moirai, on fixture files; no pipeline needed.
4. `nextflow_annotate_apply`: copy (once) and write marked blocks, `moirai/_moirai_nf.py` (helper), `moirai_overlay.config`, `moirai/job_lines.sh`.
5. `nextflow_annotate_validate`: verdict; `nextflow_annotate_selftest` proves the tool itself.

## Rules
- The hook runs when the task ends, in a subshell with all output discarded, from `trap '_moirai_end "$?"' EXIT`. It never calls `exit`: the process exit status, `errorStrategy` and the Nextflow cache are the process's own. With moirai missing or broken it does nothing.
- One activity per task, named by the task process alias (MINIMAP_A, MINIMAP_B), keyed by `meta.id`, except FASTQ_TO_FASTA (`sampleId`, the mate file stem), SEQSCREEN (`runName`), RECENTRIFUGE (`meta.id.<classifier>`); SEQKIT_STATS is keyed by `meta.id` (all1).
- Entities: inputs are the staged symlinks of the task directory, recorded by real path relative to `workflow.workDir`, so an upstream output and a downstream input get the same type and key. Outputs are the files matched by the spec globs that are newer than `.command.begin`, never symlinks, never dot files or `versions.yml`. Databases are `db/<name>` with path, size and mtime as attributes, never hashed. SeqScreen is one run directory entity plus taxonomic_results.txt, functional_results.txt, the report zip and threats_*.txt, all `contains`-related.
- Attributes: versions.yml (software entities), `task.ext.args*`, `task.cpus`, `task.container`, fastp.json counts, seqkit stats counts, size, mtime, name.
- The spec is the contract. A process in the pipeline that the spec does not list is reported, never guessed.
- Idempotent: every block sits between `# >>> moirai-agents nextflow_annotate <PROCESS>` and `# <<<` lines; apply strips all blocks and rewrites them, so nothing stacks. Stripping the blocks gives the source byte for byte.

## Constraints
- The user's source tree is read-only; everything is written inside the pipeline's provenance copy. The generated text holds no user name and no home or scratch path: every path is a run-time placeholder (`$MOIRAI_PYTHON`, `$MOIRAI_NF_HELPER`, `${projectDir}`, `workflow.workDir`).
- Groovy renders a process script before bash: in the block every bash `$` is written `\$` and a literal backslash `\\`; only `${task.process}`, `${task.cpus}`, `${task.container}`, `${workflow.workDir}` and the spec's key and path expressions are interpolated, each root name must be a process input, a variable the script assigns before `"""`, `task` or `workflow`. Free text (`task.ext.args`) travels as base64 (`.toString().bytes.encodeBase64()`) so quotes in it cannot break bash.
- Never `-resume` a run of the annotated copy against a run of the pristine one: the script text is part of the task hash.
- Nextflow is pinned (25.04.6 in the taxteller session); a hook uses no feature newer than that.
- Builds and traced runs go to compute nodes with the launcher the user approved; this capability runs nothing heavy.

## Where the hook goes
- The anchor is the first line that is exactly `"""` after `script:` and before `stub:`, skipping a `"""` that opens a `def x = ... ? """..."""` string (RECENTRIFUGE has one at its lines 54-57). The block is inserted below it with the indentation of the line that follows. Stub blocks are untouched. A script that has its own `trap ... EXIT` or an `exec` line is refused with a reason.

## Overlay and job script
- `moirai_overlay.config` is used with `nextflow run -c moirai_overlay.config`: `env` for each process (MOIRAI_PYTHON, MOIRAI_SERVICE, MOIRAI_SERVICE_SHM, MOIRAI_LOG_FILE, MOIRAI_NF_HELPER, DFTRACER_ENABLE, DFTRACER_LOG_FILE, ...), and for container engines `runOptions` that bind the helper directory, the moirai python tree (`MOIRAI_BIND_PATHS`) and `/dev/shm` (the moirai ring). It replaces an existing `runOptions`: apply reports the ones it found, and the overlay appends `$MOIRAI_BASE_RUN_OPTIONS`.
- `moirai/job_lines.sh` documents what the job script exports and runs: dftracer is always captured together with moirai (DFTRACER_ENABLE, DFTRACER_LOG_FILE under the session), one moirai-service started per node before nextflow and stopped after it (also on TERM).

## Corner cases
- `modules/local/minimap/main.nf` has an unquoted URL on its container line: a parse risk that exists in the source; the validator compares with a preview of the pristine tree.
- A task on a node without a moirai-service ring spills its events; use the local executor inside the allocation or start a service per node.
- ESVIRITU and RECENTRIFUGE use `errorStrategy ignore`: the spec has `record_on_failure` for them, so a task that exits non-zero with partial outputs is still recorded, with its exit status.
- A staged input that Nextflow copied (`stageInMode copy`) looks like a new file; the newer-than-`.command.begin` test and symlink skip cover the default (symlink) mode only.
- KAIJU_COUNTTAXONREADS and KAIJU2TABLE also read the kaiju db; the approved diagram does not draw it, so the spec does not record it (open).

## Corrector
- verdict `anchor` or `one_block`: the process was not found or the script opener is unusual: fix the spec's `file`/process name, or `shell:` processes need their own spec; then apply and validate again.
- `escapes`: a bare `$` or backslash in a block: fix the template in the tool (never edit the .nf), apply again.
- `names_resolve`: an expression names something that is not a process input: fix the spec expression (`overrides`), apply again.
- `source_intact`: stripping the blocks does not give the source: someone edited the copy; apply again (it starts from the markers).
- `bash_syntax`: the rendered block does not parse: reproduce with `nextflow_annotate_preview`, fix the tool, add a check.
- `privacy`: a path or name leaked into generated text: replace by a placeholder.
- `nextflow_parse`: annotated parse fails and the pristine passes: bisect by applying one process at a time with `exclude`.
- Same failure twice: stop and ask.

## Self-heal
First read `known_lessons` in a failed result or `lesson_search` the error text. A tool failure is repaired with `capability_update` (never by editing the copy), reproduced by a new check, then `capability_verify`.

## Self-learn
Anything explored here becomes a capability: a process shape the anchor misses, a Groovy escaping trap or a container engine mount met while annotating another Nextflow pipeline is captured with `capability_update` (or as `workload_<app>` / `software_<x>` when it concerns the app) with the run that proved it; the user's corrections to the entity plan are lessons on this capability.
