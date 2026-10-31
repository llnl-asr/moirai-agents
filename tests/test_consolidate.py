"""Finding what overlaps or contradicts, and merging it only when a person accepts."""
import hashlib

from moirai_agents.core import capstore as S
from moirai_agents.core import consolidate as Q
from moirai_agents.core import learning as L

BASE = "the moirai venv lives on lustre and is built on the login node"


def text(cap="lessons"):
    return (S.shadow(cap) / "LESSONS.md").read_text()


def test_nothing_to_consolidate_stays_quiet():
    L.add("lessons", "flux queue pdebug limits runs to one hour on tuolumne", "user")
    L.add("lessons", "hdf5 must come from source because cray headers carry a typo", "user")
    assert Q.lessons() == []


def test_overlapping_lessons_are_one_group_even_across_capabilities():
    a = L.add("lessons", BASE, "user")["id"]
    b = L.add("knowledge", BASE + " of tuolumne systems", "user")["id"]
    g = Q.lessons()[0]
    assert g["kind"] == "overlap" and g["ids"] == sorted([a, b]) and g["capabilities"] == ["knowledge", "lessons"]


def test_lessons_that_disagree_are_a_conflict():
    L.add("lessons", "always build the moirai venv on lustre from the login node", "user")
    L.add("knowledge", "never build the moirai venv on lustre from the login node", "user")
    assert Q.lessons()[0]["kind"] == "conflict"


def test_a_capability_with_too_many_lessons_is_reported():
    for i in range(Q.CROWDED + 1):
        L.add("lessons", hashlib.sha256(str(i).encode()).hexdigest(), "user")
    assert any(g["kind"] == "crowded" for g in Q.lessons())


def test_a_group_kept_on_purpose_is_not_reported_again():
    a = L.add("lessons", BASE, "user")["id"]
    b = L.add("knowledge", BASE + " of tuolumne systems", "user")["id"]
    assert Q.keep([a], "x")["status"] == "error" and Q.keep([a, b], "")["status"] == "error"
    assert Q.keep([b, a], "one is about the site, one the tool")["status"] == "ok"
    assert Q.report()["lessons"] == []


def test_a_merge_changes_nothing_until_accepted():
    a = L.add("lessons", BASE, "user")["id"]
    b = L.add("lessons", BASE + " of tuolumne systems", "user")["id"]
    m = L.merge([a, b], "build the moirai venv on the login node, on lustre", "lessons", "same fact")
    assert m["status"] == "ok" and BASE in text()
    L.decide(m["id"], True)
    rows = L.all_lessons()
    assert BASE not in text() and "build the moirai venv on the login node, on lustre" in text()
    assert rows[a]["status"] == rows[b]["status"] == "superseded" and rows[a]["superseded_by"] == m["id"]


def test_rejecting_a_merge_keeps_what_it_would_replace():
    a = L.add("lessons", BASE, "user")["id"]
    b = L.add("lessons", BASE + " of tuolumne systems", "user")["id"]
    L.decide(L.merge([a, b], "merged", "lessons", "n")["id"], False)
    assert L.all_lessons()[a]["status"] == "applied" and BASE in text()


def test_a_merge_needs_known_lessons_and_a_home():
    a = L.add("lessons", BASE, "user")["id"]
    assert L.merge([a], "t", "lessons", "n")["status"] == "error"
    assert L.merge([a, "L-none"], "t", "lessons", "n")["status"] == "error"


def test_bundled_capabilities_report_only_real_candidates():
    assert all(g["kind"] in ("overlap", "conflict") and len(g["ids"]) == 2 for g in Q.capabilities())


def test_the_same_rule_with_a_negative_in_one_is_not_a_conflict():
    assert not Q._opposed("traces are read-only and the index never sits next to them", "traces are read-only and the index goes in the pipeline")
    assert Q._opposed("never build the venv on a compute node", "build the venv on a compute node")
