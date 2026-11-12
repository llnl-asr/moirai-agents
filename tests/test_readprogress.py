import gzip
import json
import threading

from moirai_agents.core import provtrace as T
from moirai_agents.core import readprogress as RP


def _act(i):
    return {"name": "step", "cat": "moirai", "ph": 1, "pid": 1, "ts": i, "dur": 5, "args": {"relations": {"used": ["x"]}}}


def _traces(d, n=3):
    d.mkdir(parents=True, exist_ok=True)
    for k in range(n):
        (d / f"t{k}.pfw.gz").write_bytes(gzip.compress((json.dumps(_act(k)) + "\n").encode()))
    return str(d)


def test_a_step_outside_a_read_costs_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_HOME", str(tmp_path / "state"))
    with RP.step("anything", total=3) as s:
        s.advance(1)
    assert not (tmp_path / "state").exists()


def test_a_read_shows_its_steps_while_it_runs_and_logs_them(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_HOME", str(tmp_path / "state"))
    seen = {}
    with RP.op("p1", "open") as o:
        with RP.step("read records", total=2, nbytes=2000) as s:
            s.advance(1, "a.pfw.gz", 1000)
            o.write(force=True)
            seen["active"] = RP.active("p1")
    a = seen["active"][0]
    assert a["kind"] == "open" and a["steps"][0]["name"] == "read records" and a["steps"][0]["done"] == 1 and a["steps"][0]["status"] == "running"
    assert RP.active("p1") == []                               # ended: no longer active
    log = RP.recent("p1")
    assert len(log) == 1 and log[0]["status"] == "done" and log[0]["steps"][0]["secs"] >= 0
    assert RP.last("p1")["open"]["id"] == log[0]["id"]
    st = RP.stats("p1")
    assert st["reads"] == 1 and st["steps"][0]["step"] == "read records" and st["steps"][0]["mb_s"] is not None


def test_a_quick_read_of_a_fresh_index_is_not_logged_and_a_failure_is(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_HOME", str(tmp_path / "state"))
    with RP.op("p2", "index"):
        with RP.step("check index", total=1):
            pass
    assert RP.recent("p2") == []
    try:
        with RP.op("p2", "query"):
            with RP.step("query 1: x"):
                raise ValueError("boom")
    except ValueError:
        pass
    r = RP.recent("p2")
    assert r[0]["status"] == "failed" and "boom" in r[0]["error"] and r[0]["steps"][0]["status"] == "failed"


def test_reads_on_other_threads_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_HOME", str(tmp_path / "state"))
    ids = []

    def one(k):
        with RP.op("p3", f"k{k}") as o:
            with RP.step("index"):
                ids.append(o.id)
    ts = [threading.Thread(target=one, args=(k,)) for k in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(ids)) == 3 and len(RP.recent("p3")) == 3


def test_reading_traces_records_find_index_and_read_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MOIRAI_TV_INDEX_WAVE", "2")             # 3 files in waves of 2: progress per wave
    tr = _traces(tmp_path / "tr")
    with RP.op("p4", "open", tr):
        g = T.read(tr)
    assert g.counts()["activities"] == 3
    o = RP.recent("p4")[-1]
    names = [s["name"] for s in o["steps"]]
    assert names[:2] == ["find files", "wait for the index"] and "index" in names and "read records" in names
    ix = next(s for s in o["steps"] if s["name"] == "index")
    assert ix["done"] == 3 and ix["total"] == 3 and ix["bytes"] and ix["bytes_done"] == ix["bytes"]
    assert o["files"] == 3 and o["bytes"] > 0
    with RP.op("p4", "open", tr):                              # a second read: the index is fresh, nothing is indexed again
        T.read(tr)
    assert "index" not in [s["name"] for s in RP.recent("p4")[-1]["steps"]]
