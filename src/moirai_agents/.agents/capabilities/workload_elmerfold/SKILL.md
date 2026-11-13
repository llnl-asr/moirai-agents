---
name: workload_elmerfold
description: Elmerfold (LBANN OpenFold monomer-structure-prediction) provenance annotation - the 23-gap batch (MSA, templates, subprocess failures, Rabbit stage area, input chunk), launch node count, 31-char type cut. Load when annotating an elmerfold/openfold run or fixing its query gaps.
---

Cross-references: [[prov_gaps]] [[prov_annotate]] [[workload-elmerfold]] [[capability_builder]]

# workload_elmerfold

The build, launch and process layout are in dftracer's `workload-elmerfold` skill (reused, not copied). This holds only the provenance annotation.

## Rules
- Apply the batch from `elmerfold_gap_batch` (evidence: user asked to improve coverage for all 23 open gaps):
  - `load_and_merge_msas`: `capture.outputs` of sequence length and merged MSA bytes.
  - `template_featgen`: attribute and output `num_templates`, taken from the return value of `generate_template_features`.
  - activity `subprocess_finish`: patch `SubprocessTask.on_finish` (base hook, `ems/src/ems/task.py`), generating `pipeline_task` entities with `returncode` and `error`; this makes "which sequences failed the template/filter step" answerable.
  - activity `stage_setup`: statement right after `self._stage_dir = stage_dir` in `OF2StructurePredictionPipeline.__init__`, generating a `stage_area` entity with `mount_point`, `mount_device`, `mount_fstype` read from `/proc/mounts`, plus `user` and `host` inputs (the Rabbit mount).
  - `load_inputs`: capture the input chunk file.
- `gen_launch_script.py` derives `--nodes` as chunks + 1 (launch node): `elmerfold_nodes`.
- Types are cut at 31 characters in traces: check with `elmerfold_type_cut`; keep type names distinct within 31.
- The spec is always sent WHOLE to `annotate_spec`; run `elmerfold_spec_validate` on it first.

## Constraints
- Capturing must never change the app or fail it: wrap `/proc/mounts` reads and return-value inspection in try/except.
- Source tree is read-only; annotate the pipeline copy.
- Ask before re-running the smoke or baseline.

## Corner cases
- `on_finish` fires for every subclass task; the patch is on the base class, so do not also patch subclasses.
- `/proc/mounts` may list several matches: take the longest mount_point that prefixes the stage dir.
- num_templates may be 0: record 0, not absent.

## Corrector
Validator `elmerfold_spec_validate`: for each name in `missing`, add it from `elmerfold_gap_batch` to the whole spec; for `too_long_types`, shorten to 31 chars; validate again, then `annotate_spec`.

## Self-heal
- Tool failed: follow capability_builder's Self-heal; `lesson_search` the error first.
- Gap still shown after re-run: check the annotated copy contains the patch, then re-collect with `gap_collect`.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: capability_builder, Rules.
- Each closed Elmerfold gap adds its exact capture expression here.
