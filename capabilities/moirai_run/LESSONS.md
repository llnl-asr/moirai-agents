# Lessons: moirai_run

- [L-60845a5f] A flux batch job's return code does not reflect its `flux bulksubmit` chunk jobs: run.sh ends normally even when a chunk fails, so the batch is COMPLETED rc 0 and the wrapper job exits 0 (false pass). After `flux watch`, read the batch `#flux: --output` file for `exited with exit code <non-zero>` or a line ending ` failed`, and check hosts.txt exists and is non-empty.
