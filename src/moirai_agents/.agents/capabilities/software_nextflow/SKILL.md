---
name: software_nextflow
description: Nextflow / nf-core pipelines: session install on the login node with Java 17, no state in home, reading a checkout to find a small smoke command, running it on one Slurm node with rootless podman. Load for Nextflow, nf-core, main.nf, samplesheet.csv, podman wrapper, lchown, events dir.
---
Cross-references: [[app_smoke]], [[app_explore]], [[system_mammoth]], [[prov_annotate]], [[prov_performance]], [[capability_builder]]

# software_nextflow

## Rules
- Install on the login node: curl https://get.nextflow.io into <session>/tools, JAVA_HOME=/usr/lib/jvm/java-17-openjdk (default java 1.8 is too old), NXF_HOME, NXF_TEMP in the session; nothing in ~/.nextflow. nextflow_env_script renders the exports.
- Pin the version the pipeline parses: taxteller fails on 26.10.5 (strict config syntax: "`manifest` is not defined"); NXF_VER=25.04.6 works (download once with NXF_OFFLINE=false, then offline in jobs). Install plugins (nf-schema@2.3.0) once for that NXF_HOME.
- Find the smoke command from the repo: nextflow_inspect.
- Clone LLNL GitLab over SSH: ssh://git@czgitlab.llnl.gov:7999/<group>/<repo>.git.
- Run on a Slurm node (mammoth): nextflow_slurm_script renders the podman wrapper, overlay config and run_nf.sh (pass the database filesystems as extra_binds); lint with nextflow_slurm_check; the user approves the sbatch (1 node, 1 h unless told). The wrapper exists because podman 4.9 ignores XDG_RUNTIME_DIR for its events dir ("mkdir /run/user/<uid>: permission denied") and images with foreign-uid files fail with "lchown ... invalid argument" without overlay.ignore_chown_errors (no subuid range).
- A module that only declares conda and calls a tool from the host (taxteller FASTQ_TO_FASTA, seqkit) needs a container in the overlay (withName block).

## Constraints
- Source tree read-only; edits (for example skipping a process) only in the run copy. Never -A on mammoth, never --mem.

## Corner cases
- A failed task with errorStrategy terminate leaves the job waiting for the other tasks: scancel before -resume, else "Unable to acquire lock on session".
- Clean node-local podman storage with `podman unshare rm -rf`, plain rm fails.
- Registry login succeeds but pull says access denied: access is per project, not per token.

## Corrector
- nextflow_inspect_check fails: wrong directory or failed clone. Java error: use nextflow_env_script. A podman events/lchown error: regenerate with nextflow_slurm_script. Same failure twice stops the loop.

## Self-heal
- If a tool is wrong, capability_update it; never work around it with ad-hoc shell.

## Self-learn
- What is explored here becomes a capability: see capability_builder. Add what each Nextflow app teaches (profiles, executors, trap placement) as lessons; the annotation step is its own capability when approved.
