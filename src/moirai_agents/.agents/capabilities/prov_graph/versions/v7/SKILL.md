---
name: prov_graph
description: Build the provenance graph of the baseline run and gate on its health - every artifact produced and rooted in real inputs; compare plain vs traced outputs by content hash, inside tar and zstd members, separating timestamp-only differences from real ones, and record the user's acceptance of timestamp-only members. Load for the graph stage, graph_compare, "outputs identical", tar differs, compared_members 0, self-compare, timestamp_only, accept_timestamp_only, science_unchanged inconclusive.
---

Cross-references: [[prov_annotate]] [[prov_discover]] [[tools_moirai]]

# Graph health gate

## Rules
- `graph_check`, `graph_compare`, `graph_validate`. Done means all of it holds: provenance recorded, no dangling id, every artifact type present with a producer, every root a real input agreed in the spec, every type described.
- At the gate, report entity and activity counts by type and one full lineage example, root to artifact to consumers.
- Verify the run did not change the science: `graph_compare` hashes content with sha256, opens tar archives and compares member by member, and decompresses `.zst` members (zstandard module, else the `zstd` CLI, else the member is `compressed, compared as bytes`). Never trust size or whole-file equality of a tar alone.
- Compare plain against traced outputs of two different runs, e.g. `graph_compare(pipeline, plain_dir=<runs/smoke/output>, traced_dir=<runs/moirai-baseline-N/output>)`. File list: the `files` argument (relative paths) first; else every regular file under `plain_dir` when both dirs are given (cap 500, said in `notes`); else smoke's `outputs_created` (absolute entries made relative to `plain_dir`, others ignored with a note).
- A member is `timestamp_only` when after decompression every differing line matches the volatile patterns (default: lines starting `Date`/`Command`, timestamps, `/mnt/nnf/<uuid>` paths) and at most 8 lines differ; otherwise `different`.
- `science_unchanged`: pass when identical, inconclusive (named members) when only `timestamp_only` and not yet accepted, fail on `different`. The timestamp-only case is the user's call: show the members and their line counts, ask, and when the user accepts, record their acceptance with `graph_compare(..., accept_timestamp_only=<their own words>)` (same dirs and files again, only after showing the members). The verdict then passes with the words quoted.
- `accept_timestamp_only` is the user's text, at least 10 characters, never written or paraphrased by the agent or the tool. It is refused (reason in `notes`, verdict stays inconclusive) when any member is `different` or missing, `compared_members` is 0, or no member is `timestamp_only`. Every `graph_compare` call overwrites the earlier acceptance, so a rerun without the text withdraws it.
- `graph_compare_dirs` re-checks two directories by hand; `graph_compare_selftest` exercises the logic on generated tars, including the self-compare defect, the `files` argument and the acceptance states.

## Constraints
- Never call a graph healthy from counts alone: the validator decides.
- Every artifact instance has a producer activity, and its ancestors reach root inputs. The graph stays a DAG.
- Do not change the spec here to make the gate pass; fix the annotation, or ask.
- Do not widen volatile patterns to make a real difference pass; patterns are for headers the run legitimately stamps.
- Compare plain against traced outputs of two different runs. A path is never compared with itself (it is reported under `missing` with "same file as plain"), and a comparison with `compared_members` 0 is inconclusive, never identical or pass.
- Never fill `accept_timestamp_only` without the user's words; `pipeline_decide` alone does not change the verdict, the recorded acceptance does.

## Corner cases
- A file differs only by a timestamp, command line or run id: harmless, but the user's call, so the check is inconclusive until their words are recorded with `accept_timestamp_only`; it is never failed.
- Tars with equal size can still differ (equal size says nothing).
- Each tar member is read into memory; very large members cost memory.
- A partial trace set shows up as dangling ids.
- A worker that dies without finishing loses its open events; check the process's exit path.
- `outputs_created` holds absolute paths of the plain run: joined under both dirs they point at the same file. They are now made relative to `plain_dir`.
- `plain_dir` equal to `traced_dir` gives a note and nothing compared.
- `compare_timestamp_only` keeps the first 20 members; the full list is in the `graph_compare` return value.

## Parallel plan

Independent calls go out together in one message: predictor calls on different inputs, and lookups such as knowledge_search or lesson_search, do not depend on each other. Sequential, and why: the validator runs after the predictor whose result it rules on, and the corrector only after the validator's verdict. Larger fan-outs build on [[tools_parallel]] (parallel_plan / parallel_call / parallel_validate).

## Corrector
Predictors `graph_check`, `graph_compare`, validator `graph_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `recorded` | the annotated functions did not run, or the app ran outside the annotated tree |
| `no_dangling` | a process's trace is missing, or a related entity is never declared |
| `artifacts_present` | annotation gap at the writer, or the input does not reach it |
| `artifacts_produced` | add `generated` at the writer |
| `roots_are_inputs` | a producer upstream is not annotated: go back to `prov_annotate` |
| `types_described` | fix the spec's descriptions, then `annotate_apply` |
| `science_unchanged` (fail) | a real content difference in a member: inspect it with `graph_compare_dirs`; tracing may have changed behaviour |
| `science_unchanged` (inconclusive, compared_members 0 or "same file as plain") | the comparison compared a file with itself or nothing: compare plain against traced outputs of two different runs; rerun `graph_compare` with `plain_dir`, `traced_dir` of different runs and `files` (relative, e.g. `chunk_000000/<id>.tar`), or check with `graph_compare_dirs` |
| `science_unchanged` (inconclusive, only timestamp/command-line differences) | show the user the named members; if they accept, record it: rerun `graph_compare` with the same arguments plus `accept_timestamp_only=<the user's words>`, then `graph_validate`. Without their acceptance leave it inconclusive |

## Self-heal
- `graph_check` has no traces: the baseline did not finish.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A gap the gate missed: `capability_update` with a check for it.
- When a pipeline is confirmed, add its case to `prov_discover`'s `CASES.md`.

## Evidence
### graph_compare said all 10 tars `identical` but md5 differed (confirmed 2026-10-31)
Pipeline elmerfold-prov-20261030-125944: smoke vs moirai-baseline `output/chunk_000000/*.tar`, both 71680 bytes, md5 of the first tar differed (acf9c027edc98e61 vs 0c04ade021fe54e5), yet the tool reported identical. Hashing all 70 members: 60 identical, 10 `<seq>/hhsearch_output.hhr.zst` differ; after zstd decompression each differs in exactly 4 lines, the `Date` and `Command` header lines (timestamp and per-job Rabbit mount `/mnt/nnf/<uuid>/...`). Expected verdict now: inconclusive with those 10 members named. Verify: `graph_compare_selftest`.

### graph_compare reported 10 tars identical with compared_members 0 (self-compare) (confirmed 2026-11-08)
Pipeline elmerfold-prov-20261030-125944: `graph_compare(plain_dir=<runs/smoke/output>, traced_dir=<runs/moirai-baseline-3/output>)` took the file list from smoke `outputs_created`, which are absolute paths of the plain run; joining them under both dirs resolved to the same plain file, so each tar was compared with itself (compared_members 0) and `science_unchanged` stayed inconclusive. `graph_compare_dirs` with relative files `chunk_000000/<id>.tar` gave 70 members compared, 0 different, 10 `hhsearch_output.hhr` timestamp_only. Fixed: `files` argument, directory-walk default, absolute paths made relative, self-compare reported under missing. Verify: `graph_compare_selftest` (`self_compare`, `resolved`, `files_argument`).

### science_unchanged inconclusive forever on timestamp_only members (confirmed 2026-11-08)
Pipeline elmerfold-prov-20261030-125944: 70 members compared, 0 different, 10 `hhsearch_output.hhr` with Date/Command header lines differing. The skill said "the user's call" but nothing recorded the call, so `pipeline_advance` refused the inconclusive verdict even after `pipeline_decide`. Fixed: `graph_compare(accept_timestamp_only=<user's words>)` records `compare_timestamp_only_accepted`; `science_status` passes quoting it. Verify: `graph_compare_selftest` (`states.timestamp_only_accepted`, `acceptance`).
