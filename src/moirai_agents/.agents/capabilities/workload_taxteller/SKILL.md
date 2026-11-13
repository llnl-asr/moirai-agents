---
name: workload_taxteller
description: taxteller (LLNL ibap) Nextflow metagenomics pipeline: smoke copy, subsample, skipped databases, step times, registry access. Load for taxteller, kaiju, kaijux, centrifuge, seqscreen.
---
Cross-references: [[software_nextflow]], [[system_mammoth]], [[app_smoke]], [[app_explore]], [[capability_builder]]

# workload_taxteller

## Rules
- Smoke on a copy: taxteller_smoke_inputs renders the copy, the 100k read-pair subsample (400000 lines per fastq) and the samplesheets; run with software_nextflow's nextflow_slurm_script on one mammoth node.
- The kaijux database is skipped (files mode 600 owned by another user): KAIJU_KAIJUX is commented out in the copy only and the skip is stated in the plan.
- Step times on cold databases (subsample): MINIMAP_A/B about 8 min each, SEQSCREEN, CENTRIFUGER, ESVIRITU inside the hour, CENTRIFUGE_CENTRIFUGE (427 GB db) over 57 min: a smoke needs pbatch (24 h), not 1 h pdebug.
- The 5 KAIJU steps pull czregistry.llnl.gov:5050/ibap/taxteller/rcf_kaiju:1.10.1: login works but the pull is denied without membership of ibap/taxteller; ask the user for the image path and role, do not retry.

## Constraints
- Source read-only; the user approves a pbatch job (larger than 1 node, 1 h).

## Corner cases
- FASTQ_TO_FASTA declares only conda: give it the seqkit container in the overlay.

## Corrector
- taxteller_smoke_inputs_check fails: a command edits the source tree; point the sed at the run copy's src/ and render again. The same failure twice stops the loop.

## Self-heal
- If a tool here is wrong, capability_update it.

## Self-learn
- What is explored here becomes a capability: see capability_builder. Record each taxteller run's step times and blockers as lessons.
