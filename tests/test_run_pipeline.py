"""The whole run pipeline on a real Python app, with a fake moirai-service that writes real gzip members while the app runs."""
import json
import subprocess
from pathlib import Path

from conftest import fake_moirai

from moirai_agents.core import jobs as J
from moirai_agents.core import pipeline as P

APP = '''"""Doc."""
from __future__ import annotations
import sys


class Model:
    def fit(self, data_path, out):
        with open(data_path) as f:
            text = f.read()
        with open(out, "w") as f:
            f.write(text.upper())
        return out


def prepare(path):
    with open(path, "w") as f:
        f.write("abc")
    return path


if __name__ == "__main__":
    p = prepare("in.txt")
    Model().fit(p, "out.txt")
'''
SPEC = {"entity_types": [{"type": "raw", "role": "input", "description": "raw data"}, {"type": "fitted", "role": "output", "description": "the result"}],
        "activities": [{"file": "app.py", "function": "prepare", "name": "prepare", "generated": [{"type": "raw", "key_expr": "path", "store": "local_disk"}]},
                       {"file": "app.py", "function": "Model.fit", "name": "fit", "used": [{"type": "raw", "key_expr": "data_path", "store": "local_disk"}],
                        "generated": [{"type": "fitted", "key_expr": "result", "store": "local_disk"}]}]}


def setup(tmp_path, monkeypatch):
    src = tmp_path / "app"
    src.mkdir()
    (src / "app.py").write_text(APP)
    py = fake_moirai(tmp_path / "moirai")
    monkeypatch.setenv("MOIRAI_AGENTS_MOIRAI_PYTHON", str(py))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "moirai" / "fake"))
    return src


def passes(call, pid, stage):
    P.decide(pid, stage, True, "go on")
    call("pipeline_advance", pipeline=pid, stage=stage)


def test_run_pipeline_end_to_end(tmp_path, monkeypatch, call):
    src = setup(tmp_path, monkeypatch)
    pid = call("pipeline_new", kind="run", name="demo", source=str(src), command="python3 app.py")["id"]

    o = call("explore_run", pipeline=pid)
    assert o["app"]["languages"] == {"python": 1} and o["app"]["entry_points"] == ["app.py"] and o["system"]["moirai"]["ok"]
    v = call("explore_validate", pipeline=pid)
    assert [f["name"] for f in v["failed"]] == ["dftracer_utils"], v   # not in the fake Python: the query stage needs it, so it is reported
    P.record(pid, "explore", validation={"status": "pass"})
    passes(call, pid, "explore")

    r = call("smoke_run", pipeline=pid, wait=30)
    assert r["rc"] == 0, r
    assert call("smoke_validate", pipeline=pid)["status"] == "pass"
    assert "out.txt" in P.stage(P.load(pid), "smoke")["outputs"]["outputs_created"]
    passes(call, pid, "smoke")

    scan = call("annotate_scan", pipeline=pid)
    assert {c["function"] for c in scan["candidates"]} >= {"prepare", "Model.fit"}
    assert call("annotate_spec", pipeline=pid, spec={"activities": [{"name": "x", "file": "no.py"}]})["status"] == "error"
    assert call("annotate_spec", pipeline=pid, spec=SPEC)["status"] == "ok"
    before = (src / "app.py").read_text()
    a = call("annotate_apply", pipeline=pid)
    assert a["status"] == "ok" and "@_moirai_prov.activity('fit'" in a["diff"]
    assert (src / "app.py").read_text() == before               # the user's tree is never touched
    text = Path(a["annotated"], "app.py").read_text()
    assert text.splitlines()[1] == "from __future__ import annotations" and "import _moirai_prov" in text.splitlines()[2]
    assert call("annotate_validate", pipeline=pid)["status"] == "pass"
    passes(call, pid, "annotate")

    assert call("integrate_apply", pipeline=pid)["status"] == "ok"
    v = call("integrate_validate", pipeline=pid)
    assert v["status"] == "pass", v
    passes(call, pid, "integrate")

    r = call("moirai_smoke_run", pipeline=pid, wait=30)
    assert r["rc"] == 0, r
    v = call("moirai_validate", pipeline=pid, stage="moirai_smoke")
    assert v["status"] == "pass", v
    assert P.load(pid)["dashboard"]["live"] is True
    passes(call, pid, "moirai_smoke")

    r = call("moirai_baseline_run", pipeline=pid, command="python3 app.py", wait=30)
    assert r["rc"] == 0, r
    assert call("moirai_validate", pipeline=pid, stage="baseline")["status"] == "pass"
    passes(call, pid, "baseline")

    s = call("query_summary", pipeline=pid)
    assert (s["activities"], s["entities"], s["edge_kinds"]) == (2, 2, {"generated": 2, "used": 1})
    assert call("query_entities", pipeline=pid, type="fitted")["count"] == 1
    assert call("query_lineage", pipeline=pid, entity="fitted")["hops"] >= 1
    assert call("query_validate", pipeline=pid)["status"] == "pass"
    passes(call, pid, "query")
    assert P.summary(P.load(pid))["current"] is None


def test_validator_catches_a_run_that_recorded_nothing(tmp_path, monkeypatch, call):
    src = setup(tmp_path, monkeypatch)
    pid = call("pipeline_new", kind="run", name="demo", source=str(src), command="true")["id"]
    for s in ("explore", "smoke", "annotate"):
        P.update(pid, lambda c, s=s: c["stages"][s].update(status="passed"))
    call("annotate_spec", pipeline=pid, spec=SPEC)
    call("annotate_apply", pipeline=pid)
    call("integrate_apply", pipeline=pid)
    P.update(pid, lambda c: c["stages"]["integrate"].update(status="passed"))
    assert call("moirai_smoke_run", pipeline=pid, wait=30)["rc"] == 0
    v = call("moirai_validate", pipeline=pid, stage="moirai_smoke")
    assert v["status"] == "fail" and {f["name"] for f in v["failed"]} >= {"trace", "activities"}


def test_jobs_have_a_time_limit_and_service_stops(tmp_path, monkeypatch):
    pid = P.new("analysis", "t", traces="/t")["id"]
    marker = tmp_path / "stopped"
    j = J.start(pid, "ingest", "slow", f"trap 'touch {marker}' EXIT\nsleep 30 & wait\n", str(tmp_path), limit=1)
    r = J.wait(pid, j["id"], 15)
    assert r["status"] == "done" and r["timed_out"] and marker.exists()


def test_c_annotation_compiles_with_moirai_macros(tmp_path, call, monkeypatch):
    src = tmp_path / "c"
    src.mkdir()
    (src / "m.c").write_text('#include <stdio.h>\n/* void fake(void) { */\nstatic int save(const char *path) {\n  FILE *f = fopen(path, "w");\n  fclose(f);\n  return 0;\n}\nint main(void) {\n  return save("o.txt");\n}\n')
    pid = call("pipeline_new", kind="run", name="c", source=str(src), command="./a.out")["id"]
    spec = {"entity_types": [{"type": "o", "role": "output", "description": "d"}], "entry": "m.c",
            "activities": [{"file": "m.c", "function": "save", "name": "save", "generated": [{"type": "o", "key_expr": "path", "store": "local_disk"}]}]}
    assert call("annotate_spec", pipeline=pid, spec=spec)["status"] == "ok"
    a = call("annotate_apply", pipeline=pid)
    assert a["status"] == "ok" and not a["missing"], a
    t = Path(a["annotated"], "m.c").read_text()
    assert "MOIRAI_C_ACTIVITY_START(save);" in t and 'moirai_activity_relate_entity(moirai_save, MOIRAI_REL_GENERATED, "o", path, MOIRAI_STORE_LOCAL_DISK, path);' in t
    assert t.index("moirai_initialize(NULL)") > t.index("int main") and "moirai_entity_type(\"o\", MOIRAI_ROLE_OUTPUT" in t
    inc = Path(__file__).resolve().parents[2] / "moirai" / "include"
    if inc.exists():
        r = subprocess.run(["gcc", "-fsyntax-only", f"-I{inc}", f"-I{inc.parent / 'build' / 'include'}", str(Path(a["annotated"], "m.c"))], capture_output=True, text=True)
        assert r.returncode == 0 or "dftracer" in r.stderr, r.stderr
