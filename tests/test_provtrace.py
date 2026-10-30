import gzip
import json

from moirai_agents.core import provtrace as T


def rec(name="ET", **a):
    return {"name": name, "cat": "dftracer", "ph": 4, "pid": 1, "args": a}


def act(i, **rel):
    return {"name": "step", "cat": "moirai", "ph": 1, "pid": 1, "ts": i, "dur": 5, "args": {"relations": rel}}


def member(*recs):
    return gzip.compress("".join(json.dumps(r) + "\n" for r in recs).encode())


def test_tail_reads_only_whole_members_and_resumes(tmp_path):
    f = tmp_path / "t.pfw.gz"
    one, two = member(rec(type="a", role=1)), member(act(1, used=["x"]), act(2, generated=["y"]))
    f.write_bytes(one + two[:10])                      # the second member is still being written
    t = T.Tail(str(tmp_path))
    assert [r["name"] for r in t.poll()] == ["ET"]
    assert t.poll() == []
    with open(f, "ab") as fh:
        fh.write(two[10:])
    assert [r["ts"] for r in t.poll()] == [1, 2]
    assert t.poll() == []


def test_tail_waits_at_a_hole_left_by_an_earlier_member(tmp_path):
    """Members land at reserved offsets: a later one can be complete before an earlier one."""
    f = tmp_path / "t.pfw.gz"
    a, b = member(act(1)), member(act(2))
    f.write_bytes(b"\0" * len(a) + b)
    t = T.Tail(str(tmp_path))
    assert t.poll() == []
    with open(f, "r+b") as fh:
        fh.write(a)
    assert [r["ts"] for r in t.poll()] == [1, 2]


def test_new_files_are_found_while_tailing(tmp_path):
    t = T.Tail(str(tmp_path))
    assert t.poll() == []
    (tmp_path / "a_host1.pfw.gz").write_bytes(member(act(1)))
    (tmp_path / "a-1234-spill.pfw.gz").write_bytes(member(act(2)))
    assert sorted(r["ts"] for r in t.poll()) == [1, 2]


def test_graph_lineage_and_dangling():
    g = T.Graph()
    g.extend([rec(type="raw", role=1, description="d"), rec("EH", id="a", type="raw", store=3, uri="file:///in"), rec("EH", id="b", type="m", store=2, uri="file:///m"),
              rec("EH", id="c", type="o", store=2, uri="file:///out"), rec("EH", id="a", type="raw", store=3, uri="file:///in"),
              act(1, used=["a"], generated=["b"]), act(2, used=["b"], generated=["c"]), rec("ER", relation=16, subject="c", object="zz")])
    n = g.counts()
    assert (n["activities"], n["entities"], n["edges"], n["relations"], n["dangling"]) == (2, 3, 4, 1, 1)
    back = g.lineage("file:///out")
    assert back["reached"] == 3 and [s["activity"] for s in back["steps"]] == ["step", "step"]
    assert [e["id"] for e in back["ends"]] == ["a"]
    assert g.lineage("a", "forward")["reached"] == 3
    assert g.summary()["by_store"] == {"parallel_fs": 1, "local_disk": 2}


def test_older_prov_category_reads_the_same():
    g = T.Graph()
    g.add({"name": "x", "cat": "PROV", "ph": 1, "pid": 1, "ts": 1, "args": {"relations": {"used": ["a"]}}})
    assert g.counts()["activities"] == 1


def fh(path, h):
    return {"name": "FH", "cat": "dftracer", "ph": 4, "pid": 1, "args": {"name": path, "value": h}}


def io(name, ts, h, **a):
    return {"name": name, "cat": "POSIX", "ph": 1, "pid": 1, "ts": ts, "dur": 1, "args": {"fhash": h, **a}}


def span(name, ts, dur):
    return {"name": name, "cat": "moirai", "ph": 1, "pid": 1, "ts": ts, "dur": dur, "args": {"relations": {}}}


def test_io_arriving_before_its_activity_is_attributed_when_it_ends():
    g = T.Graph()
    out = g.extend([fh("/data/in.h5", "f1"), io("read", 15, "f1", ret=100), io("read", 16, "f1", ret=50), span("load", 10, 20)])
    assert [d for d in out if d["kind"] == "access"] == [{"kind": "access", "aid": "1:10", "path": "/data/in.h5", "op": "read"}]
    assert g.accesses() == [{"aid": "1:10", "path": "/data/in.h5", "op": "read", "calls": 2, "bytes": 150}]


def test_io_goes_to_the_innermost_activity_either_way():
    g = T.Graph()
    g.extend([fh("/d/a", "f"), io("write", 25, "f"), span("inner", 20, 10), span("outer", 10, 40), io("write", 26, "f")])
    assert {a["aid"]: a["calls"] for a in g.accesses()} == {"1:20": 2}


def test_file_name_arriving_after_the_call_still_shows():
    g = T.Graph()
    out = g.extend([span("a", 10, 20), io("open64", 15, "f"), fh("/d/late", "f")])
    assert out[-1] == {"kind": "access", "aid": "1:10", "path": "/d/late", "op": "open"}


def test_interpreter_and_system_files_are_dropped_and_aggregates_count():
    g = T.Graph()
    g.extend([fh("/usr/lib/x.py", "p"), fh("/proc/self/maps", "q"), fh("/data/a", "d"), span("a", 10, 20), io("read", 12, "p"), io("read", 12, "q"),
              {"name": "__lxstat64", "cat": "POSIX", "ph": 3, "pid": 1, "ts": 13, "args": {"fhash": "d", "dft_cnt": 4}}])
    assert g.accesses() == [{"aid": "1:10", "path": "/data/a", "op": "meta", "calls": 4, "bytes": 0}]
