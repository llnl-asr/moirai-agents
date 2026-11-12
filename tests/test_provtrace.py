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
    first = [r["ts"] for r in t.poll()]               # TraceViewer skips the hole: the complete later member may come first, never a misread one
    assert first in ([], [2])
    with open(f, "r+b") as fh:
        fh.write(a)
    second = [r["ts"] for r in t.poll()]
    assert sorted(first + second) == [1, 2]            # every record once, none twice
    assert t.poll() == []


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


def _count(f):
    return T.row_counts(str(f)).get("moirai:step", 0)


def test_index_follows_a_growing_trace_and_skips_unchanged_files(tmp_path, monkeypatch):
    """A live run's trace grows: each read first rebuilds the index for what changed, and an unchanged trace does not open the indexer."""
    f = tmp_path / "t.pfw.gz"
    f.write_bytes(member(act(1)))
    assert _count(tmp_path) == 1
    with open(f, "ab") as fh:
        fh.write(member(act(2), act(3)))
    assert T.stale([str(f)]) == [str(f)]
    assert _count(tmp_path) == 3
    kept = T._load_stamps(T.index_dir([str(f)]))
    kept[str(f)]["young"] = False                      # settled: nothing left to rebuild
    T._save_stamps(T.index_dir([str(f)]), kept)
    assert T.stale([str(f)]) == []
    opened = []
    monkeypatch.setattr(T, "_build", lambda *a: opened.append(a) or ([], []))
    T.ensure_index([str(f)])
    assert opened == []


def test_a_same_size_rewrite_in_the_same_clock_tick_is_reindexed(tmp_path):
    """A member written into a reserved hole changes neither size nor (within the filesystem's mtime tick) mtime: a file indexed while young
    is rebuilt once more on the next read."""
    f = tmp_path / "t.pfw.gz"
    a, b = member(act(1)), member(act(2))
    f.write_bytes(a + b"\0" * len(b))
    st = f.stat()
    assert _count(tmp_path) == 1
    with open(f, "r+b") as fh:
        fh.seek(len(a))
        fh.write(b)
    import os
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # the same stamp as when it was indexed
    assert T.stale([str(f)]) == [str(f)]               # indexed while young
    assert _count(tmp_path) == 2


def test_a_file_that_cannot_be_indexed_does_not_stop_the_others(tmp_path, monkeypatch):
    good, bad = tmp_path / "a.pfw.gz", tmp_path / "b.pfw.gz"
    good.write_bytes(member(act(1)))
    bad.write_bytes(member(act(2)))
    U = T._tv()
    real = U.Indexer

    def indexer(files, **kw):                           # b.pfw.gz is "being written": indexing it fails
        if str(bad) in files:
            raise RuntimeError("truncated member")
        return real(files=files, **kw)
    monkeypatch.setattr(U, "Indexer", indexer)
    T.ensure_index(T.expand(str(tmp_path)))
    kept = T._load_stamps(T.index_dir([str(good)]))
    assert str(good) in kept and str(bad) not in kept    # the good file is indexed; the bad one has no stamp, so it is retried
    assert str(bad) in T.stale([str(bad)])
    monkeypatch.setattr(U, "Indexer", real)
    T.ensure_index(T.expand(str(tmp_path)))
    assert str(bad) in T._load_stamps(T.index_dir([str(bad)]))


def test_keeper_reindexes_a_live_trace_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "KEEP_EVERY_S", 0.05)
    f = tmp_path / "t.pfw.gz"
    f.write_bytes(member(act(1)))
    k = T.keep_indexed(str(tmp_path))
    k.every = 0.05
    assert T.keep_indexed(str(tmp_path)) is k           # one keeper per traces, touched
    import time
    deadline = time.time() + 20
    while T.stale([str(f)]) and time.time() < deadline:
        T._save_stamps(T.index_dir([str(f)]), {p: {**v, "young": False} for p, v in T._load_stamps(T.index_dir([str(f)])).items()})
        time.sleep(0.1)
    assert str(f) in T._load_stamps(T.index_dir([str(f)]))
    with open(f, "ab") as fh:
        fh.write(member(act(2)))
    deadline = time.time() + 20
    while T._load_stamps(T.index_dir([str(f)]))[str(f)]["stamp"][0] != f.stat().st_size and time.time() < deadline:
        time.sleep(0.05)
    assert T._load_stamps(T.index_dir([str(f)]))[str(f)]["stamp"][0] == f.stat().st_size
    k.idle = 0                                          # nobody watching: it stops and leaves the registry
    k.join(timeout=5)
    assert not k.is_alive() and str(tmp_path) not in T._KEEPERS
