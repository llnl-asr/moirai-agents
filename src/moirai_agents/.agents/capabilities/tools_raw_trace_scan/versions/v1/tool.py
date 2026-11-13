"""Scan raw provenance traces (*.pfw.gz) for attribute entity records, with no TraceViewer index (no RocksDB LOCK). Files are read by a thread pool (gzip releases the GIL); the module is loaded dynamically so a process pool cannot pickle its functions."""
import glob
import gzip
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _pat(attr):
    return re.compile(r'"uri":"' + re.escape(attr) + r'=([0-9.eE+-]+)","store":\d+,"type":"([A-Za-z0-9_]+)\.attribute","id":"([0-9a-f]+)"')


def _one(args):
    f, attr, etype = args
    pat, out = _pat(attr), []
    with gzip.open(f, "rt", errors="replace") as fh:
        for line in fh:
            if attr + "=" in line:
                m = pat.search(line)
                if m and (not etype or m.group(2) == etype):
                    out.append((m.group(2), m.group(3), float(m.group(1))))
    return out


def _scan(trace_dir, attr, etype, workers):
    files = sorted(glob.glob(str(Path(trace_dir).expanduser() / "*.pfw.gz")))
    tot, sums, ids, lo, hi = {}, {}, {}, {}, {}
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(_one, [(f, attr, etype) for f in files]):
            for t, i, v in out:
                tot[t] = tot.get(t, 0) + 1
                sums[t] = sums.get(t, 0.0) + v
                ids.setdefault(t, {})[i] = v
                lo[t] = min(v, lo.get(t, v))
                hi[t] = max(v, hi.get(t, v))
    return {"files": len(files), "by_type": {t: {"records": tot[t], "distinct_ids": len(ids[t]), "mean_over_records": sums[t] / tot[t],
            "mean_over_distinct": sum(ids[t].values()) / len(ids[t]), "min": lo[t], "max": hi[t]} for t in tot}}


def register(mcp):
    @mcp.tool()
    def raw_attribute_scan(trace_dir: str, attribute: str = "mean_plddt", entity_type: str = "", workers: int = 16) -> dict:
        """Read every *.pfw.gz in trace_dir (a pipeline's traces/.../raw) and aggregate the attribute entity records `<attribute>=<number>`
        (type `<entity>.attribute`). Does not use the TraceViewer index, so it works while that index is locked or building. entity_type
        limits it to one entity (e.g. best_structure). Identical values share one attribute id, so both the mean over records and over
        distinct ids are returned: state which one you quote. The result is NOT an evidence_answer: say so to the user.
        Returns {status, files, by_type: {type: {records, distinct_ids, mean_over_records, mean_over_distinct, min, max}}}."""
        if not Path(trace_dir).expanduser().is_dir():
            return {"status": "error", "error": f"{trace_dir} is not a directory", "fix": "pass the raw trace dir of the pipeline"}
        r = _scan(trace_dir, attribute, entity_type, max(1, min(workers, 32)))
        if not r["files"]:
            return {"status": "error", "error": "no *.pfw.gz files in " + trace_dir}
        return {"status": "ok", **r}

    @mcp.tool()
    def raw_scan_selftest() -> dict:
        """Self-check on a synthetic trace: two best_structure mean_plddt records must give mean 80.0, min 70.0, max 90.0. Returns {status, got}."""
        with tempfile.TemporaryDirectory() as d:
            with gzip.open(Path(d) / "a.pfw.gz", "wt") as fh:
                for i, v in enumerate((70.0, 90.0)):
                    fh.write('{"name":"EH","args":{"uri":"mean_plddt=%s","store":0,"type":"best_structure.attribute","id":"%016x"}}\n' % (v, i))
            r = _scan(d, "mean_plddt", "best_structure", 2)["by_type"].get("best_structure", {})
        ok = r.get("mean_over_records") == 80.0 and r.get("min") == 70.0 and r.get("max") == 90.0
        return {"status": "ok" if ok else "error", "got": r}
