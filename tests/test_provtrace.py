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
