# Lessons: prov_discover

- [L-0519104a] Nextflow support for discover_scan is staged as proposal 680de3c96080: .nf files get family "nf" (process/workflow name as the enclosing function), patterns for path/file declarations (open), publishDir (write), params.*db/input/index/ref/match/samplesheet/fasta/genome (read) and tool command lines or podman run (exec); work/ and .nextflow/ are skipped; discover_inventory reads the smoke data_dir (the Slurm run's output dir) before cwd. The discovery record of taxteller is workspaces/taxteller/20261112_222121/provenance/discovery.md (the agent's report printed a wrong parent path: check with ls).

- [L-2a7fc4fc] discover_scan scans 0 files on a Nextflow pipeline (.nf/.config/.sh) so touch_points fails; scan patterns need Nextflow/bash support (process input/output path/val blocks, publishDir, script commands), else read modules by hand and record with evidence.
