---
name: software_spindle_probe
description: Probe whether a command hangs or trips a Spindle assertion under default Spindle versus SPINDLE_FLUXOPT=disable. Load before testing a Spindle-related hang on a small case.
---
Cross-references: [[workload_biotox]], [[software_flux_nested]], [[system-tuolumne-spindle]]

# software_spindle_probe

## Rules
- spindle_probe_script renders the flux batch script only; the user approves submission (node time).
- Run the small case first (1 node, 16 runs per arm, 8 at a time), then the big-run conditions; read both with spindle_probe_summary.
- Report `reproduced: false` as an unproven cause, never as a fix.

## Constraints
- Watchdog exit code 124 means the command hung. Output lines look like `arm i rc=<rc> secs=<s> spindle_assert=<n>`.

## Corner cases
- BioTox prepack: 32 of 32 runs rc 0, 0 assertions on a small case (see workload_biotox lessons); the hang needs the big-run conditions or is intermittent.
- Unset moirai-venv entries from LD_LIBRARY_PATH in the submitting shell.

## Corrector
- A rendered script that fails `bash -n` is repaired in the tool with capability_update, never edited by hand; render and check again.

## Self-heal
- A summary with no arms means the output format changed: update the regex in the tool.

## Self-learn
- What is explored here becomes a capability: see capability_builder. Add a lesson to workload_biotox with the summary numbers after each probe.
