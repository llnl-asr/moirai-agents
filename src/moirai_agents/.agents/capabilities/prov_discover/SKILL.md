---
name: prov_discover
description: Find the scientific artifact and every place the app touches data, and prepare the questions that decide what provenance to capture. Load for the discover stage, or any question about what should be captured as provenance.
---

# Discover

Provenance records **what data exists and how it became what it is**. It is data-centric only: no PAPI, power, GPU profilers, node counters or timing. Function-level POSIX events may stay on, but the deliverable is the graph.

In scope, every place the app touches data:

| verb | examples |
|---|---|
| access | read a file, HDF5, netCDF or DB row, load a model, parse an input deck |
| store | write an output, checkpoint or plot, insert a DB row, save a model |
| compute | turn entity A into B in memory: solve, fold, train step, filter, reduce |
| communicate | MPI send/recv/collective, NCCL, host-to-device copy, RPC, queue, Ray object |

## Rules
- Start from the plain run. `discover_cases` first, then `discover_inventory`, `discover_scan`.
- The artifact is what a scientist would cite or analyze: predicted structure, simulation field or snapshot, trained model and its metrics, variant calls, a catalog, the table under a figure. NOT logs, timing files, scratch or caches.
- Propose 1 to 3 candidates with evidence: file pattern, writer function, size. Read the README and the writers; walk callers from the writer.
- Walk backwards from the artifact writer: each argument it writes came from a producer, each producer consumed inputs. Stop at roots: input files, configs, seeds, downloaded records, model weights. Then walk forwards: who reads the artifact.
- Rule of thumb: track all scientific artifacts, everything that leads to them and everything derived from them.
- Describe every type as you name it, in the domain's words, the ones a user would type in a question, not code identifiers.
- `discover_record`, then `ask_user`: which artifacts, which types upstream and downstream, granularity, exclusions.

## Constraints
- Never pick the entity set silently. The user decides; this stage ends with questions.
- Nothing changes here: no spec, no annotation.
- Read only the lines the scan names; do not read whole trees.

## Corner cases
- Granularity: one entity per unit the scientist reasons about (a sample, a timestep, a structure), not per array element. If a loop makes millions, the batch or step is the entity, with the members as a range in its key (`samples[0:4096]@epoch=3`). Ask when unclear.
- Optional config inputs are still entities. When a config file is absent, name what configured the run (`preset:<name>`), or the root silently disappears from lineage.
- Per-step entities explode at fine cadence: record only steps that are written or consumed downstream.
- A data file written by a subprocess: the driver records it after the subprocess ends.
- Outputs that exist only on some paths (`allow_failure`): record them only if the file exists.

## Corrector
Predictors `discover_inventory`, `discover_scan`, `discover_record`, validator `discover_validate`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| `inventory` | run it on where the data landed: `output_dir` |
| `touch_points` | the app uses a library the patterns miss: add the pattern with `capability_update` |
| `candidates` | read the writers, then `discover_record` with evidence |
| `questions` | ask what only the user can settle |

## Self-heal
- `discover_inventory` finds nothing: the run wrote elsewhere; ask where.
- A tool failed: follow `capability_builder`'s Self-heal.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- After a pipeline is confirmed, add a case to `CASES.md` through `capability_update`: artifact, entity types with id schemes, activities, traps. No names or paths of people.
- A pattern the scan missed: `capability_update` with a check for it.
