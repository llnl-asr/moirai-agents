import copy
import hashlib
import re

import pytest

from moirai_agents.core import diagrams as D
from moirai_agents.core import pipeline as P
from moirai_agents.webui import api

SPEC = {
    "scientific_artifacts": ["out"],
    "entity_types": [{"type": "raw", "role": "input"}, {"type": "mid", "role": "intermediate"}, {"type": "out", "role": "output"}],
    "activities": [
        {"file": "m.py", "name": "merge", "function": "merge", "used": [{"type": "raw"}], "generated": [{"type": "mid", "when": "ok"}]},
        {"file": "m.py", "name": "save", "mode": "patch", "target": "S.go", "used": [{"type": "mid"}], "generated": [{"type": "out"}],
         "invalidated": [{"type": "mid"}], "relations": [{"relation": "contains", "subject": {"type": "out"}, "object": {"type": "mid"}}]},
    ],
}


def run():
    return P.new("run", "app", "/src", "./run")["id"]


def test_a_graph_with_faults_says_which():
    bad = {"nodes": [{"id": "a"}, {"id": "a"}, {"id": "b", "parent": "zz"}], "edges": [{"source": "a", "target": "nope"}]}
    names = {n for n, _, _ in D.graph_problems(bad)}
    assert {"node_ids", "parents", "edge_ends"} <= names
    assert D.graph_problems({"nodes": [], "edges": []})[0][0] == "not_empty"
    cyc = {"nodes": [{"id": "a", "parent": "b"}, {"id": "b", "parent": "a"}], "edges": []}
    assert "parent_cycle" in {n for n, _, _ in D.graph_problems(cyc)}
    assert D.graph_problems({"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}]}) == []


def test_the_graph_of_a_spec_counts_what_the_spec_says():
    g = D.from_spec(SPEC)
    assert D.graph_problems(g) == []
    kinds = sorted(e["kind"] for e in g["edges"])
    assert kinds == ["generated", "generated", "invalidated", "relation", "used", "used"]
    assert sum(1 for e in g["edges"] if e["conditional"]) == 1
    assert sum(1 for n in g["nodes"] if n["kind"] == "artifact") == 1
    assert {n["parent"] for n in g["nodes"] if n.get("parent")} == {"f:m.py"}


def test_publishing_is_a_pipeline_output_and_does_not_invalidate_a_verdict():
    pid = run()
    rev = P.load(pid)["stages"]["annotate"]["rev"]
    e = D.publish_graph(pid, "annotate", "annotation", "What it covers", D.from_spec(SPEC), "2 types")
    c = P.load(pid)
    assert e["nodes"] == 6 and e["edges"] == 6
    assert c["stages"]["annotate"]["outputs"]["diagrams"] == ["annotation"]
    assert c["stages"]["annotate"]["rev"] == rev                       # presentation only
    assert [r["diagrams"] for r in P.summary(c)["stages"] if r["name"] == "annotate"] == [1]
    assert [d["name"] for d in P.stage_state(c, "annotate")["diagrams"]] == ["annotation"]
    assert P.stage_state(c, "explore")["diagrams"] == []
    with pytest.raises(P.PipelineError):
        D.publish_graph(pid, "nope", "x", "t", D.from_spec(SPEC))
    with pytest.raises(P.PipelineError):
        D.publish_graph(pid, "annotate", "../x", "t", D.from_spec(SPEC))
    with pytest.raises(P.PipelineError):
        D.read(c, "missing")


def test_an_edit_is_kept_beside_the_original_and_group_boxes_are_not_reported_as_removed():
    pid = run()
    D.publish_graph(pid, "annotate", "annotation", "t", D.from_spec(SPEC))
    c = P.load(pid)
    orig = D.read(c, "annotation")["graph"]
    # what the page saves: the group box is not drawn, so it is not in the edit; one box removed, one added, one renamed
    files = {n["id"]: n["label"] for n in orig["nodes"] if n["kind"] == "file"}
    kept = [dict({k: v for k, v in n.items() if k != "parent"}, **({"group": files[n["parent"]]} if n.get("parent") else {})) for n in orig["nodes"]
            if n["id"] not in files and n["id"] != "t:raw"]                 # the page's dgFlatten: a parent becomes a `group` name
    edited = copy.deepcopy({"nodes": kept + [{"id": "u1", "label": "extra"}], "edges": [e for e in orig["edges"] if "t:raw" not in (e["source"], e["target"])]})
    edited["nodes"][0]["label"] = "renamed"
    r = D.save_edit(pid, "annotation", edited)
    assert r["changes"]["removed_nodes"] == ["raw"] and r["changes"]["added_nodes"] == ["extra"] and len(r["changes"]["renamed"]) == 1
    assert "m.py" not in r["changes"]["removed_nodes"]
    d = D.read(c, "annotation")
    assert d["edited"] and d["graph"] == orig and d["edited_ts"]
    assert D.reset_edit(pid, "annotation")["edited"] is False
    d = D.read(c, "annotation")
    assert d["edited"] is None and "edited_ts" not in d
    with pytest.raises(P.PipelineError):
        D.save_edit(pid, "annotation", {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "zz"}]})


def test_publishing_again_replaces_the_diagram_and_drops_the_old_edit():
    pid = run()
    D.publish_graph(pid, "annotate", "annotation", "t", D.from_spec(SPEC))
    g = D.read(P.load(pid), "annotation")["graph"]
    D.save_edit(pid, "annotation", {"nodes": g["nodes"][:3], "edges": []})
    D.publish_graph(pid, "annotate", "annotation", "t2", D.from_spec(SPEC))
    d = D.read(P.load(pid), "annotation")
    assert d["title"] == "t2" and d["edited"] is None and len(D.listing(P.load(pid))) == 1


def test_a_bokeh_item_is_checked_and_served_with_the_installed_version():
    bokeh = pytest.importorskip("bokeh")
    from bokeh.embed import json_item
    from bokeh.plotting import figure
    f = figure(height=100)
    f.vbar(x=["a"], top=[1], width=0.5)
    item = json_item(f, "c")
    assert D.bokeh_problems(item) == []
    assert D.bokeh_problems({**item, "version": "1.0.0"})[0][0] == "version"
    assert D.bokeh_problems({"doc": {}})[0][0] == "shape"
    pid = run()
    D.publish_bokeh(pid, "annotate", "c", "Timing", item)
    assert D.read(P.load(pid), "c")["item"]["target_id"] == "c"


def test_the_vendored_page_libraries_are_the_ones_the_notice_names():
    vendor = api.R.STATIC / "vendor"
    notice = (vendor / "THIRD_PARTY.md").read_text()
    for name in api.VENDOR:
        sha = hashlib.sha256((vendor / name).read_bytes()).hexdigest()
        assert re.search(rf"`{re.escape(name)}`.*`{sha}`", notice), f"{name} does not match the hash in THIRD_PARTY.md"
    for name in ("MERMAID_LICENSE",):
        assert not (vendor / name).exists()
    assert "GPLv3" in notice and not (vendor / "cytoscape-svg.js").exists()      # GPL code is not vendored
    for name in ("/diagrams.js", "/moirai.js"):
        assert (api.R.STATIC / name.lstrip("/")).is_file()


def test_bokeh_files_the_server_may_send_exist_in_the_installed_package():
    bokeh = pytest.importorskip("bokeh")
    from pathlib import Path
    js = Path(bokeh.__file__).parent / "server" / "static" / "js"
    assert (js / "bokeh.min.js").is_file()
    assert "../x" not in api.BOKEH_JS and all("/" not in n for n in api.BOKEH_JS)
