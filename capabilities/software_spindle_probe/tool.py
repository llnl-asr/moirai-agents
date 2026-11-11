"""Spindle probe: render a flux batch probe script and summarize its output."""
import re
from pathlib import Path


def register(mcp):
    @mcp.tool()
    def spindle_probe_script(workdir: str, command: str, input_file: str, bank: str, nodes: int = 1, minutes: int = 30, runs: int = 16, parallel: int = 8, watchdog_s: int = 900, queue: str = "pbatch") -> dict:
        """Render (do not submit) a flux batch script that runs `command` `runs` times per arm, `parallel` at a time, once with default Spindle and once with --env=SPINDLE_FLUXOPT=disable. `command` is the program and its arguments (run as `flux run -N 1 -n 1 -c 12 <command>` in a per-run dir holding a copy of input_file). Each run logs 'arm i rc=<rc> secs=<s> spindle_assert=<n>'. Returns {status, script} or {status: error, problem}."""
        if not (Path(workdir).is_absolute() and Path(input_file).is_file()):
            return {"status": "error", "problem": "workdir must be absolute and input_file must exist"}
        if nodes < 1 or runs < 1 or parallel < 1 or minutes < 1 or watchdog_s < 1:
            return {"status": "error", "problem": "nodes, runs, parallel, minutes and watchdog_s must be positive"}
        text = f"""#!/bin/bash
# flux: -N {nodes}
# flux: -t {minutes}m
# flux: --queue={queue}
# flux: --bank={bank}
# flux: --exclusive
# flux: --job-name=spindle-probe
# flux: --output={workdir}/batch-{{{{id}}}}.out
D={workdir}
echo "probe start $(date) on $(hostname)"
one() {{
    arm=$1; i=$2; w=$D/$arm/run_$i; mkdir -p "$w"; cp {input_file} "$w/input.pdb"; cd "$w" || exit 1
    extra=""
    [ "$arm" = "nospindle" ] && extra="--env=SPINDLE_FLUXOPT=disable"
    s=$SECONDS
    timeout {watchdog_s} flux run -N 1 -n 1 -c 12 $extra {command} > out.txt 2> err.txt
    rc=$?
    echo "$arm $i rc=$rc secs=$((SECONDS - s)) spindle_assert=$(grep -c 'Spindle.*Assertion\\|ldcs_audit_server' err.txt)"
}}
export -f one; export D
for arm in default nospindle; do
    echo "== arm $arm start $(date)"
    seq 1 {runs} | xargs -P {parallel} -I{{}} bash -c "one $arm {{}}"
    echo "== arm $arm end $(date)"
done
echo "probe end $(date)"
"""
        return {"status": "ok", "script": text}

    @mcp.tool()
    def spindle_probe_summary(output_text: str) -> dict:
        """Summarize a probe batch output (text): per arm the number of runs, nonzero rc (124 = watchdog hang), Spindle assertion total, and min/max seconds. Returns {status, arms, reproduced} where reproduced is true when any run has rc != 0 or an assertion."""
        arms = {}
        for m in re.finditer(r"^(\w+)(?: \w+)? (\d+) rc=(\d+) secs=(\d+) spindle_assert=(\d+)", output_text, re.M):
            a = arms.setdefault(m.group(1), {"runs": 0, "nonzero_rc": 0, "assertions": 0, "secs": []})
            a["runs"] += 1
            a["nonzero_rc"] += int(m.group(3) != "0")
            a["assertions"] += int(m.group(5))
            a["secs"].append(int(m.group(4)))
        for a in arms.values():
            s = a.pop("secs")
            a["min_s"], a["max_s"] = min(s), max(s)
        bad = any(a["nonzero_rc"] or a["assertions"] for a in arms.values())
        return {"status": "ok", "arms": arms, "reproduced": bad}
