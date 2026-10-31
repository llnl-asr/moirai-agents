"""A person accepts, rejects or refines what the agents learnt: lessons and capabilities alike."""
from conftest import tools
from moirai_agents.core import capstore as S
from moirai_agents.core import learning as L
from test_capabilities import AGENT, CHECKS, PVC, SKILL, TOOL


def lessons_file():
    return (S.shadow("lessons") / "LESSONS.md").read_text()


def pending():
    return L.add("lessons", "the first wording", "agent", "run 1")["id"]


def test_a_pending_lesson_is_edited_in_place_then_accepted():
    lid = pending()
    assert L.revise(lid, "a better wording", note="too vague", by="me") == {"status": "ok", "id": lid, "applied": False}
    assert "better" not in lessons_file()
    o = L.all_lessons()[lid]
    assert o["status"] == "pending" and o["revisions"][0]["note"] == "too vague"
    L.decide(lid, True)
    assert f"[{lid}] a better wording" in lessons_file()


def test_save_and_accept_is_one_step():
    lid = pending()
    assert L.revise(lid, "a better wording", approve=True, by="me")["applied"]
    assert L.all_lessons()[lid]["status"] == "applied" and "a better wording" in lessons_file()


def test_a_rejected_lesson_reopens_with_an_edit():
    lid = pending()
    L.decide(lid, False)
    L.revise(lid, "a better wording")
    assert L.all_lessons()[lid]["status"] == "pending"


def test_a_correction_replaces_an_applied_lesson_only_when_accepted():
    lid = pending()
    other = L.add("lessons", "a lesson about something else entirely", "user")["id"]
    L.decide(lid, True)
    new = L.revise(lid, "the corrected wording", note="wrong")["id"]
    assert new != lid and "first wording" in lessons_file() and L.all_lessons()[lid]["status"] == "applied"
    L.decide(new, True)
    text = lessons_file()
    assert "first wording" not in text and "corrected wording" in text and f"[{other}]" in text
    assert L.all_lessons()[lid]["status"] == "superseded"
    assert L.revise(lid, "again")["status"] == "error"


def test_rejecting_a_correction_keeps_the_lesson():
    lid = pending()
    L.decide(lid, True)
    L.decide(L.revise(lid, "the corrected wording")["id"], False)
    assert "first wording" in lessons_file() and L.all_lessons()[lid]["status"] == "applied"


def test_a_lesson_moves_to_another_capability():
    lid = pending()
    L.decide(lid, True)
    L.revise(lid, "", "knowledge", approve=True)
    assert "first wording" not in lessons_file() and "first wording" in (S.shadow("knowledge") / "LESSONS.md").read_text()


def test_the_agent_can_refine_but_not_accept():
    lid = pending()
    r = tools()["lesson_revise"]["fn"](id=lid, text="a better wording", note="asked to")
    assert r["applied"] is False and L.all_lessons()[lid]["status"] == "pending"


def proposal():
    return S.propose("demo_cap", TOOL, SKILL, AGENT, CHECKS, "d", "w", pvc=PVC)["id"]


def test_refining_a_proposal_replaces_it_and_keeps_the_note():
    pid = proposal()
    r = S.refine(pid, SKILL.replace("\nr\n", "\na sharper rule\n"), note="too thin", by="me")
    assert r["status"] == "ok"
    rows = S.list_capabilities()["pending"]
    assert [p["id"] for p in rows] == [r["id"]] and "sharper" in rows[0]["skill"] and rows[0]["revisions"][0]["note"] == "too thin"
    assert S.refine(pid)["status"] == "error"
    assert S.apply(r["id"], True)["status"] == "ok"
    assert "sharper" in S.get("demo_cap")["skill"]


def test_a_refused_refinement_leaves_the_proposal_pending():
    pid = proposal()
    assert "problems" in S.refine(pid, SKILL.replace("## Corrector\nc\n", ""))
    assert [p["id"] for p in S.list_capabilities()["pending"]] == [pid]


def test_a_live_capability_changes_only_when_approved():
    assert S.revise("lessons", note="")["status"] == "error"
    g = S.get("lessons")
    r = S.revise("lessons", g["skill"] + "\n<!-- a change -->\n", note="add a note")
    assert r["kind"] == "update" and "a change" not in S.get("lessons")["skill"]
    assert S.apply(r["id"], True)["status"] == "ok"
    assert "a change" in S.get("lessons")["skill"]


def test_the_panels_reach_it_through_the_server(web):
    w = web()
    rows = w.get("/api/capabilities")["active"]
    assert rows
    lid = w.post("/api/lesson/revise", {"id": "L-none", "text": "x"})
    assert lid["status"] == "error"
    assert w.post("/api/capability/refine", {"name": "lessons"})["status"] == "error"
