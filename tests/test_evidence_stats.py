"""stats: a recorded value's mean, median and range over every entity that records it, computed from all the records with TraceViewer."""
import gzip
import json

from moirai_agents.core import evidence as E


def eh(i, t, u):
    return {"name": "EH", "cat": "dftracer", "ph": "M", "pid": 1, "args": {"id": i, "type": t, "uri": u, "store": 2}}


def er(s, o):
    return {"name": "ER", "cat": "dftracer", "ph": "M", "pid": 1, "args": {"relation": 16, "subject": s, "object": o}}


def traces(d):
    """Three best_structure values (a1 shared by two structures: identical values share one entity) and one of another type."""
    recs = [{"name": "ET", "cat": "dftracer", "ph": "M", "pid": 1, "args": {"type": "best_structure", "role": 2}}]
    for i, x in {"a1": 80.0, "a2": 60.5, "a3": 90.25}.items():
        recs.append(eh(i, "best_structure.attribute", f"mean_plddt={x}"))
    recs += [eh("z", "structure_metrics.attribute", "mean_plddt=10"), er("z", "m0"), eh("m0", "structure_metrics", "file:///m0.json")]
    for n, (s, c) in enumerate([("a1", 2), ("a2", 1), ("a3", 1)]):
        for j in range(c):
            recs += [eh(f"s{n}{j}", "best_structure", f"file:///s{n}{j}.pdb"), er(s, f"s{n}{j}")]
    recs.append({"name": "relax", "cat": "moirai", "ph": "X", "pid": 1, "ts": 5, "dur": 1, "args": {"relations": {"generated": ["s00"]}}})
    (d / "t.pfw.gz").write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in recs).encode()))
    return d


def test_stats_counts_every_entity_and_skips_the_catalog(tmp_path):
    r = E.run(str(traces(tmp_path)), [{"op": "stats", "name": "mean_plddt", "type": "best_structure"}, {"op": "stats", "name": "mean_plddt"}],
              str(tmp_path / "idx"))
    one, both = r["outs"]
    g = one["groups"][0]
    assert (g["of"], g["n"], g["distinct"], g["min"], g["max"], g["median"]) == ("best_structure", 4, 3, 60.5, 90.25, 80.0)
    assert round(g["mean"], 4) == round((80 * 2 + 60.5 + 90.25) / 4, 4)
    assert "4 best_structure entities: mean 77.69" in r["steps"][0]["headline"] and "3 distinct values" in r["steps"][0]["headline"]
    assert [x["of"] for x in both["groups"]] == ["best_structure", "structure_metrics"]
    assert r["steps"][0]["cites"][0]["id"] == "a2" and r["steps"][0]["cites"][1]["id"] == "a3"        # the lowest and highest records first
    assert r["stats"]["entities_declared"] is None                                                      # every entity was never loaded
    assert not r["gaps"]


def test_stats_of_an_unrecorded_value_is_a_gap(tmp_path):
    r = E.run(str(traces(tmp_path)), [{"op": "stats", "name": "rmsd"}], str(tmp_path / "idx"))
    assert r["outs"][0]["groups"] == [] and r["gaps"][0]["kind"] == "not_recorded"


def test_synthesis_numbers_read_as_people_write_them(tmp_path):
    computed = "mean_plddt of 99,801 best_structure entities: mean 79.00, median 80.12, from 21.29 to 98.31."
    ok = E.check_synthesis("On the 32-node run the mean pLDDT is 79.00 over 99,801 best structures [S1], from 21.29 to 98.31 [S1]. "
                           "Pipeline elmerfold-live-32n-20261109-140945, run 2026-11-09T14:09.", 1, computed, "What is the mean pLDDT on the 32-node run?")
    assert ok["ok"], ok
    bad = E.check_synthesis("The mean pLDDT is 79.5 over 1,234 structures [S1].", 1, computed)
    assert bad["problems"] == ["the number 79.5 is not in the computed result", "the number 1,234 is not in the computed result"]


def test_stats_names_the_entity_that_owns_the_lowest_and_highest_value(tmp_path):
    r = E.run(str(traces(tmp_path)), [{"op": "stats", "name": "mean_plddt", "type": "best_structure"}], str(tmp_path / "idx"))
    g = r["outs"][0]["groups"][0]
    assert g["min_owner"] == ["file:///s10.pdb"] and g["max_owner"] == ["file:///s20.pdb"], g
    assert "Lowest on file:///s10.pdb; highest on file:///s20.pdb" in r["steps"][0]["headline"]
