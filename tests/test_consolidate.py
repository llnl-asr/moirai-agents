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


ALWAYS = "always build the moirai venv on lustre from the login node"
NEVER = "never build the moirai venv on lustre from the login node"


def _conflict():
    a = L.add("lessons", ALWAYS, "user")["id"]
    b = L.add("knowledge", NEVER, "user")["id"]
    return a, b


def _opts(a, b):
    return [{"label": f"{a} holds", "do": "supersede", "winner": a}, {"label": "both hold", "do": "keep"},
            {"label": "one lesson", "do": "merge", "text": "build the moirai venv on lustre from a compute node", "capability": "lessons"}]


def test_a_question_hides_its_group_and_the_badge_counts_it():
    a, b = _conflict()
    assert Q.report()["lessons"]
    r = Q.ask([a, b], "Which holds: always or never build on the login node?", _opts(a, b), 0, "the later run built it there")
    assert r["status"] == "ok"
    rep = Q.report()
    assert rep["lessons"] == [] and [q["id"] for q in rep["questions"]] == [r["id"]]
    assert rep["questions"][0]["lessons"][a]["text"] == ALWAYS          # the user reads them in full


def test_a_bad_question_is_refused():
    a, b = _conflict()
    assert Q.ask([a, b], "q?", [{"label": "x", "do": "supersede", "winner": "L-nope"}, {"label": "k", "do": "keep"}], 0, "w")["status"] == "error"
    assert Q.ask([a, b], "q?", [{"label": "k", "do": "keep"}], 0, "w")["status"] == "error"
    assert Q.ask([a, b], "q?", _opts(a, b), 7, "w")["status"] == "error"


def test_picking_supersede_retires_the_other_and_clears_everything():
    a, b = _conflict()
    q = Q.ask([a, b], "Which holds?", _opts(a, b), 0, "evidence")["id"]
    r = Q.answer(q, 0, "", "me")
    assert r["status"] == "ok" and r["applied"] == "supersede"
    rows = L.all_lessons()
    assert rows[a]["status"] == "applied" and rows[b]["status"] == "superseded" and rows[b]["superseded_by"] == a
    assert NEVER not in text("knowledge")
    rep = Q.report()
    assert rep["lessons"] == [] and rep["questions"] == []
    assert Q.answer(q, 0)["status"] == "error"                         # answered once


def test_picking_merge_applies_one_lesson_in_place_of_both():
    a, b = _conflict()
    q = Q.ask([a, b], "Which holds?", _opts(a, b), 2, "evidence")["id"]
    assert Q.answer(q, 2, "", "me")["status"] == "ok"
    rows = L.all_lessons()
    assert rows[a]["status"] == rows[b]["status"] == "superseded"
    assert "compute node" in text("lessons") and Q.report()["lessons"] == []


def test_picking_keep_stops_reporting_the_group():
    a, b = _conflict()
    q = Q.ask([a, b], "Which holds?", _opts(a, b), 1, "evidence")["id"]
    Q.answer(q, 1, "they are about different clusters", "me")
    assert Q.report()["lessons"] == [] and L.all_lessons()[b]["status"] == "applied"


def test_your_own_words_go_to_the_agent_until_it_closes_them():
    a, b = _conflict()
    q = Q.ask([a, b], "Which holds?", _opts(a, b), 0, "evidence")["id"]
    assert Q.answer(q, None, "")["status"] == "error"
    assert Q.answer(q, None, "neither: use the build node", "me")["status"] == "for_agent"
    assert [x["id"] for x in Q.questions("for_agent")] == [q] and Q.report()["questions"] == []
    assert Q.close(q, "revised both")["status"] == "ok" and Q.questions("for_agent") == []


def test_a_proposed_merge_clears_the_group_while_it_waits():
    a = L.add("lessons", BASE, "user")["id"]
    b = L.add("knowledge", BASE + " of tuolumne systems", "user")["id"]
    assert Q.report()["lessons"]
    L.merge([a, b], BASE + " (tuolumne)", "lessons", "same fact")
    assert Q.report()["lessons"] == []


def test_the_web_server_compares_capabilities_in_a_child_and_only_when_one_changed(tmp_path, monkeypatch):
    """consolidate.capabilities() is about a minute of pure Python: in the server it held the GIL and requests waited behind it."""
    import json
    import os
    import subprocess

    from moirai_agents.webui import consolidation as WC
    runs, real = [], subprocess.run

    def child(cmd, **kw):
        if cmd[1:2] != ["-m"]:
            return real(cmd, **kw)                     # git, for the upstream layers
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps([{"kind": "overlap", "ids": ["a_cap", "b_cap"], "texts": ["x"]}]), "")
    monkeypatch.setattr(WC.subprocess, "run", child)
    monkeypatch.setattr(WC.consolidate, "capabilities", lambda: (_ for _ in ()).throw(AssertionError("compared in the server's process")))
    c = WC.Consolidation(tmp_path)
    assert c.report()["capabilities_pending"] and c.report()["capabilities"] == []
    assert c.refresh_capabilities() and runs[-1][1:] == ["-m", "moirai_agents.webui.consolidation"]
    rep = c.report()
    assert rep["capabilities"][0]["ids"] == ["a_cap", "b_cap"] and not rep["capabilities_pending"]
    assert not c.refresh_capabilities() and len(runs) == 1                     # nothing changed: no child
    assert not WC.Consolidation(tmp_path).refresh_capabilities() and len(runs) == 1   # nor after a restart
    d = tmp_path / "project_caps" / "new_cap"
    d.mkdir(parents=True)
    (d / "capability.json").write_text(json.dumps({"name": "new_cap", "version": 1}))
    (d / "SKILL.md").write_text("x")
    assert os.environ["MOIRAI_AGENTS_PROJECT_CAPABILITIES"] == str(tmp_path / "project_caps")
    assert c.refresh_capabilities() and len(runs) == 2                         # a capability changed: compared again
    L.add("lessons", BASE, "user")
    L.add("knowledge", BASE + " of tuolumne systems", "user")
    assert c.report()["lessons"] and len(runs) == 2                             # lessons are cheap: fresh, in-process


def test_the_child_prints_the_capability_groups(monkeypatch, capsys):
    from moirai_agents.webui import consolidation as WC
    monkeypatch.setattr(WC.consolidate, "capabilities", lambda: [{"kind": "overlap", "ids": ["a", "b"], "texts": []}])
    assert WC.main() == 0 and '"ids": ["a", "b"]' in capsys.readouterr().out
