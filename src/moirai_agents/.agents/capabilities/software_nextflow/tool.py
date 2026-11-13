"""software_nextflow: session environment, checkout inspection and Slurm single-node run scripts for Nextflow / nf-core pipelines."""
import os


def register(mcp):
    @mcp.tool()
    def nextflow_env_script(tools_dir: str, java_home: str = "/usr/lib/jvm/java-17-openjdk") -> dict:
        """Render shell exports that run Nextflow with Java 17 and all its state under tools_dir (never home). Returns {status, script}."""
        t = tools_dir.rstrip("/")
        lines = [
            f"export JAVA_HOME={java_home}",
            f"export PATH={java_home}/bin:{t}:$PATH",
            f"export NXF_HOME={t}/nxf_home",
            f"export NXF_TEMP={t}/nxf_tmp",
            "export NXF_OFFLINE=${NXF_OFFLINE:-true}",
        ]
        return {"status": "ok", "script": "\n".join(lines) + "\n"}

    @mcp.tool()
    def nextflow_inspect(path: str) -> dict:
        """Read a Nextflow checkout: main.nf, test profile, samplesheet and module names. Returns {status, main_nf, test_config, samplesheet, modules}."""
        p = path.rstrip("/")
        mods = []
        for root in ("modules/local", "modules/nf-core"):
            d = os.path.join(p, root)
            if os.path.isdir(d):
                mods += sorted(os.listdir(d))
        ss = os.path.join(p, "assets", "samplesheet.csv")
        rows = []
        if os.path.isfile(ss):
            rows = open(ss).read().splitlines()[:5]
        return {
            "status": "ok",
            "main_nf": os.path.isfile(os.path.join(p, "main.nf")),
            "test_config": os.path.isfile(os.path.join(p, "conf", "test.config")),
            "samplesheet": rows,
            "modules": mods,
        }

    @mcp.tool()
    def nextflow_inspect_check(path: str) -> dict:
        """Validate that path is a Nextflow checkout (main.nf present). Returns {status: pass|fail, detail}."""
        ok = os.path.isfile(os.path.join(path, "main.nf"))
        return {"status": "pass" if ok else "fail", "detail": "main.nf found" if ok else "no main.nf"}

    @mcp.tool()
    def nextflow_slurm_script(session: str, run_dir: str, src_dir: str, nxf_ver: str = "25.04.6",
                              extra_binds: str = "", java_home: str = "/usr/lib/jvm/java-17-openjdk") -> dict:
        """Render, as text only (nothing is written or submitted), the three files that run a Nextflow pipeline with -profile podman on ONE Slurm node with rootless podman: the podman wrapper (node-local graph/run/tmp, --events-backend=file, overlay.ignore_chown_errors), the config overlay (extra_binds: comma-separated database filesystems to bind-mount) and run_nf.sh (usage: run_nf.sh <samplesheet> <tag>; NXF_RESUME=1 adds -resume). Returns {status, podman_wrapper, overlay_config, run_nf_sh, submit}, submit being the sbatch line template (no -A, no --mem)."""
        s = session.rstrip("/")
        r = run_dir.rstrip("/")
        binds = " ".join(f"-v {b}:{b}" for b in extra_binds.split(",") if b)
        wrapper = (
            "#!/usr/bin/env bash\n"
            "# podman 4.9 on compute nodes ignores XDG_RUNTIME_DIR for events: pass node-local paths as options\n"
            'if [ -n "${POD_T:-}" ]; then\n'
            '  exec /usr/bin/podman --root "$POD_T/graph" --runroot "$POD_T/run" --tmpdir "$POD_T/tmp" '
            '--events-backend=file --storage-opt overlay.ignore_chown_errors=true "$@"\n'
            "fi\n"
            'exec /usr/bin/podman "$@"\n'
        )
        overlay = (
            "// session overlay, not part of the pipeline\n"
            f"podman.runOptions = '--runtime crun --group-add keep-groups {binds}'\n"
        )
        run = (
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f"S={s}; R={r}\n"
            "SHEET=$1; TAG=$2\n"
            f"export JAVA_HOME={java_home} PATH=$S/tools/bin:{java_home}/bin:$S/tools:$PATH\n"
            f"export NXF_VER={nxf_ver} NXF_HOME=$S/tools/nxf_home NXF_TEMP=/tmp/$USER-nxf-$SLURM_JOB_ID NXF_OFFLINE=true NXF_ANSI_LOG=false\n"
            "mkdir -p $NXF_TEMP\n"
            "T=/tmp/$USER-pod-$SLURM_JOB_ID; mkdir -p $T/graph $T/run $T/xdg $T/tmp; chmod 700 $T/xdg\n"
            "export POD_T=$T XDG_RUNTIME_DIR=$T/xdg REGISTRY_AUTH_FILE=$S/tools/auth/auth.json\n"
            f"cd {src_dir.rstrip('/')}\n"
            "nextflow run main.nf ${NXF_RESUME:+-resume} -profile podman -process.executor=local -c $R/mammoth_overlay.config \\\n"
            "  --input $SHEET --outdir $R/out_$TAG -w $R/work_$TAG \\\n"
            "  -with-trace $R/logs/trace_$TAG.txt > $R/logs/nextflow_$TAG.log 2>&1\n"
            "rc=$?\n"
            "podman unshare rm -rf $T/graph 2>/dev/null; rm -rf $T $NXF_TEMP\n"
            "exit $rc\n"
        )
        submit = f"sbatch --parsable -N1 -p <pdebug|pbatch> -t <limit> -J <name> -o {r}/logs/slurm_%j.out {r}/run_nf.sh <samplesheet> <tag>"
        return {"status": "ok", "podman_wrapper": wrapper, "overlay_config": overlay, "run_nf_sh": run, "submit": submit}

    @mcp.tool()
    def nextflow_slurm_check(script_text: str) -> dict:
        """Lint a run_nf.sh text: pinned NXF_VER, podman wrapper first on PATH, POD_T node-local storage, registry auth file, no home state. Returns {status: pass|fail, problems}."""
        problems = []
        for needle, msg in (("NXF_VER=", "Nextflow version not pinned"),
                            ("tools/bin:", "podman wrapper not first on PATH"),
                            ("POD_T=", "node-local podman storage (POD_T) not set"),
                            ("REGISTRY_AUTH_FILE=", "registry auth file not exported"),
                            ("NXF_HOME=", "NXF_HOME not set (would use ~/.nextflow)")):
            if needle not in script_text:
                problems.append(msg)
        if "--mem" in script_text:
            problems.append("do not request --mem on mammoth (RealMemory=1)")
        return {"status": "fail" if problems else "pass", "problems": problems}
