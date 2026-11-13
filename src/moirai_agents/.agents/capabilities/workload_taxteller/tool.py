"""workload_taxteller: smoke-copy commands for the taxteller Nextflow pipeline."""


def register(mcp):
    @mcp.tool()
    def taxteller_smoke_inputs(source: str, run_dir: str, reads: int = 100000, skip_process: str = "KAIJU_KAIJUX") -> dict:
        """Render shell commands (text only, nothing runs) that make a taxteller smoke copy: cp -a of source to run_dir/src, comment out the skip_process call in workflows/taxteller.nf of the COPY only, cut the example R1/R2 fastq.gz to `reads` read pairs (4 lines each) and write samplesheet_sub.csv and samplesheet_full.csv. Returns {status, commands}."""
        s = source.rstrip("/")
        r = run_dir.rstrip("/")
        n = int(reads) * 4
        cmds = [
            f"mkdir -p {r}/data {r}/out {r}/work {r}/logs; rm -rf {r}/src; cp -a {s} {r}/src",
            f"sed -i 's|^\\(\\s*\\){skip_process} (|\\1// (skipped) {skip_process} (|; s|^\\(\\s*\\)ch_versions.mix({skip_process}.out.versions)|\\1// ch_versions.mix({skip_process}.out.versions)|' {r}/src/workflows/taxteller.nf",
            f"D={s}/assets/example_dataset",
            f"for r in R1 R2; do zcat $D/one_tenth_subset_CDPH_P1005_S5_${{r}}.fastq.gz | head -n {n} | gzip > {r}/data/sub_${{r}}.fastq.gz; done",
            f"printf 'sample,fastq_1,fastq_2\\nSUBSAMPLE_PE,%s/data/sub_R1.fastq.gz,%s/data/sub_R2.fastq.gz\\n' {r} {r} > {r}/samplesheet_sub.csv",
            f"printf 'sample,fastq_1,fastq_2\\nSAMPLE_PAIRED_END,%s/one_tenth_subset_CDPH_P1005_S5_R1.fastq.gz,%s/one_tenth_subset_CDPH_P1005_S5_R2.fastq.gz\\n' $D $D > {r}/samplesheet_full.csv",
        ]
        return {"status": "ok", "commands": cmds}

    @mcp.tool()
    def taxteller_smoke_inputs_check(commands: list) -> dict:
        """Check rendered commands edit only the run copy (never the source tree's workflow file). Returns {status: pass|fail, problems}."""
        problems = [c[:60] for c in commands if "sed -i" in c and "/src/workflows/" not in c]
        return {"status": "fail" if problems else "pass", "problems": problems}
