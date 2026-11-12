"""A small synthetic trace in dftracer's format with every aspect in it (provenance records, the run context, outcomes, timed activities,
POSIX I/O, a CPU counter, and dftracer's metadata), for the aspect capabilities' selftests. Read back only through TraceViewer."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

H, F = "a1b2c3d4e5f60718", "f00dfeedf00dfeed"
UH, GH = "0123456789abcdef", "fedcba9876543210"      # hashed user and group; the identity records name them


def _r(_name, _cat, _ph, pid=1, ts=0, dur=0, **args):
    return {"name": _name, "cat": _cat, "ph": _ph, "pid": pid, "tid": pid, "ts": ts, "dur": dur, "args": {"hhash": H, **args}}


def records():
    t0 = 1_800_000_000_000_000
    recs = [_r("HH", "dftracer", 4, name="node01", value=H), _r("FH", "dftracer", 4, name="/data/in.csv", value=F),
            _r("SH", "dftracer", 4, name="python;app.py;--n=2", value="5151515151515151"),
            _r("start", "dftracer", 1, ts=t0, ppid=0, date="Mon Jan 01 00:00:00 2027", cmd_hash="5151515151515151", exec_hash="5151515151515151", cwd=F, version=20003),
            _r("ET", "dftracer", 4, type="raw", role=1, description="the input table"),
            _r("ET", "dftracer", 4, type="table", role=2, description="the cleaned table"),
            _r("EH", "dftracer", 4, id="r", type="raw", store=3, uri="/data/in.csv"),
            _r("EH", "dftracer", 4, id="t", type="table", store=3, uri="/data/out.csv"),
            _r("EH", "dftracer", 4, id="q1", type="table.attribute", store=0, uri="score=0.9"),
            _r("EH", "dftracer", 4, id="o1", type="clean.output", store=0, uri="returncode=0"),
            _r("EH", "dftracer", 4, id="o2", type="plot.output", store=0, uri="returncode=2"),
            _r("EH", "dftracer", 4, id="u1", type="run_context.attribute", store=0, uri="user_hash=" + UH),
            _r("EH", "dftracer", 4, id="g1", type="run_context.attribute", store=0, uri="group_hash=" + GH),
            _r("EH", "dftracer", 4, id="i1", type="identity", store=0, uri=f"{UH}=alice"),
            _r("EH", "dftracer", 4, id="i2", type="identity", store=0, uri=f"{GH}=lab"),
            _r("CM", "dftracer", 4, name="user_hash", value=UH), _r("CM", "dftracer", 4, name="group_hash", value=GH),
            _r("CM", "dftracer", 4, name="env.OMP_NUM_THREADS", value="8"),
            _r("CM", "dftracer", 4, name="env.MOIRAI_LOG_FILE", value=f"/home/@user:{UH}/run"),
            _r("EH", "dftracer", 4, id="j1", type="run_context.attribute", store=0, uri="job=f1234"),
            _r("ER", "dftracer", 4, subject="q1", object="t", relation=19),
            _r("clean", "moirai", 1, ts=t0 + 1_000_000, dur=3_000_000, relations={"used": ["r"], "generated": ["t", "o1"]}),
            _r("plot", "moirai", 1, pid=2, ts=t0 + 2_000_000, dur=1_000_000, relations={"used": ["t"], "generated": ["o2"]}),
            _r("read", "POSIX", 1, ts=t0 + 1_100_000, dur=500_000, fhash=F, count=4096, ret=4096),
            _r("write", "POSIX", 1, ts=t0 + 1_700_000, dur=250_000, fhash=F, count=1024, ret=1024),
            _r("cpu", "sys", 2, pid=0, ts=t0, user_pct=50.0, system_pct=10.0, iowait_pct=5.0, idle_pct=35.0)]
    return recs


def anonymize(recs):
    """What anonymizing a trace does: drop the identity records (the names behind the hashes)."""
    return [r for r in recs if not (r["name"] == "EH" and r["args"].get("type") == "identity")]


def write(d, anonymized: bool = False) -> str:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    recs = anonymize(records()) if anonymized else records()
    (d / "selftest.pfw.gz").write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in recs).encode()))
    return str(d)
