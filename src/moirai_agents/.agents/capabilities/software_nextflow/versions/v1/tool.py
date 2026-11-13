"""software_nextflow: session environment and checkout inspection for Nextflow / nf-core pipelines."""
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
