---
name: prov_accountability
description: Accountability aspect of provenance - who did what, when, and with what: the person and their group, job, hosts, processes, commands, tools and versions. Load when a question or a check is about who ran something, their group, the job, where and when it ran, or which tools and versions took part, and when annotating so the run records it.
---

Cross-references: [[prov_read]] [[prov_annotate]] [[prov_query]] [[privacy_guard]]

# Accountability

Scope: Who did what, when and with what: the person running and their group, the job, the hosts and processes (with their parent, start time, executable, command line and working directory), the app and its functions, the tools and versions that took part, and the temporal order of the work.

## Rules
- Answer a user's question with `evidence_answer` and these operations (each step is a TraceViewer query through prov_read; the answer is kept with the trace):
  - `context` {}: who (user and group, kept as hashes and resolved through the identity records or dftracer's string table), job, hosts, processes, tools
  - `timeline` {name?}: when each activity ran (first and last start, runs, hosts)
  - `configuration` {match?}: how each process was configured: moirai's process metadata (env.MOIRAI_*/DFTRACER_*/OMP_*, launcher job/rank/size, devices, app.* values), settings that differ between processes named
- `accountability_summary` gives the standard picture of a run; `accountability_check` says what the annotation did not capture, with the fix.
- What the annotation must record for this aspect: a run_context entity with user, group, job id, host, launch command, app version and tool versions (prov_annotate's helper records it at start); dftracer metadata on (HH, SH, FH, start and end records).
- User, group, command line and working directory are kept only as hashes (user_hash=, group_hash=, command_hash=, cwd_hash= on the run_context entity); the names behind them are `identity` entities related to nothing. To anonymize a trace, drop the identity records (and dftracer's SH/FH string tables): every hash stays, no id dangles.
- An anonymized trace answers 'anonymized user <hash>': never guess a name.
- Every number comes from a step; cite it as [S n]. What the traces do not record is a gap, not a guess.

## Constraints
- Read traces only through prov_read (core.provtrace / the evidence engine): never open or parse a trace file, never import dftracer.utils here.
- This capability owns accountability only: a question that is about another aspect goes to that aspect's capability (prov_traceability, prov_accountability, prov_decision, prov_performance).
- Never write a user or group name in plain text into a lesson, a capability or a report of another user's run: use the hash.

## Corner cases
- dftracer may also carry user_hash/group_hash/job as process metadata (CM records, through moirai.metadata once moirai has it) or in its start record: context reads all of them.
- A run on several hosts has one run_context per process: users and groups are listed once each.

## Corrector
Validator `accountability_check` (and `accountability_selftest` for the capability itself).

| Failed check | Correct |
|---|---|
| `who_ran_it` | the run_context entity is missing: the helper of prov_annotate records it at start; re-annotate (annotate_apply) and re-run |
| `processes_recorded`, `hosts_named` | keep dftracer metadata on (MOIRAI_INC_METADATA=1): start and HH records |
| `activities_timed` | annotate the functions as activities |

## Self-heal
- A tool failed: read its error, fix the input (pipeline id, trace dir); if the operation itself is wrong, follow `capability_builder`'s Self-heal and update core.aspects.accountability with its selftest.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A new kind of accountability question that needed a hand-built plan becomes an operation in core.aspects.accountability with a selftest case.
