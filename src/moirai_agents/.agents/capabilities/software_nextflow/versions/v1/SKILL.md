---
name: software_nextflow
description: Nextflow / nf-core pipelines: session install on the login node with Java 17, no state in home, reading a checkout to find a small smoke command. Load for Nextflow, nf-core, main.nf, samplesheet.csv.
---
Cross-references: [[app_smoke]], [[app_explore]], [[prov_annotate]], [[prov_performance]], [[capability_builder]]

# software_nextflow

## Rules
- Install on the login node, no compute node needed: curl https://get.nextflow.io into <session>/tools, run with JAVA_HOME=/usr/lib/jvm/java-17-openjdk (default java is 1.8, too old; 17 and 21 exist), NXF_HOME, NXF_TEMP, NXF_WORK inside the session. Nothing goes to ~/.nextflow. Verified: nextflow 26.10.5. Render the exports with nextflow_env_script.
- Find the smoke command from the repo, not from the user: nextflow_inspect reads main.nf, conf/test.config, assets/samplesheet.csv and modules/.
- Clone LLNL GitLab over SSH: https on lc.llnl.gov asks for credentials and fails in the agent; ssh://git@czgitlab.llnl.gov:7999/<group>/<repo>.git worked (lc.llnl.gov:7999 refused).
- Plugins (nf-schema) may need NXF_OFFLINE=false once, then NXF_PLUGINS_DIR in the session.
- Annotation (planned, not built): a bash EXIT trap at the top of each process script block in a COPY of the .nf files, like prov_annotate's Maestro yaml_step block; dftracer traces are always captured too.

## Constraints
- Source tree read-only; work on a copy. Runs go to compute nodes by flux, one node and one hour unless the user says more.

## Corner cases
- Containers (-profile podman) may not exist on the compute node: settle in explore.
- Databases (centrifuge, kaiju) live on lustre paths in nextflow.config: record, do not copy.

## Corrector
- nextflow_inspect_check fails: fix the path (no main.nf means the wrong directory or a failed clone), then inspect again. A Java error from the launcher: set JAVA_HOME to 17 via nextflow_env_script. The same failure twice stops the loop.

## Self-heal
- If a tool here is wrong, capability_update it; never work around it with ad-hoc shell.

## Self-learn
- What is explored here becomes a capability: see capability_builder. Add what each Nextflow app teaches (profiles, executors, trap placement) as lessons, and build the annotation step as its own capability when approved.
