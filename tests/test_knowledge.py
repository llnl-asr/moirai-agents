"""Finding what is known: the graph first, direct reads for what it cannot match."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from moirai_agents.core import knowledge as K
from moirai_agents.core import learning as L

needs_graphify = pytest.mark.skipif(not K._graphify(), reason="graphify is not installed")


@pytest.fixture
def learnt(tmp_path, monkeypatch):
    monkeypatch.setenv("MOIRAI_AGENTS_LEARNING_DIR", str(tmp_path / "learning"))
    user = tmp_path / "caps"
    monkeypatch.setenv("MOIRAI_AGENTS_USER_CAPABILITIES", str(user))
    d = user / "system_demo"
    d.mkdir(parents=True)
    (d / "capability.json").write_text('{"name": "system_demo", "enabled": true}')
    (d / "SKILL.md").write_text("---\nname: system_demo\n---\nCross-references: [[software-mpi]]\n\n## Scheduler\n\n### flux run hangs without the pdebug queue\nUse -q pdebug.\n\n### other\nx\n\n## Storage\ny\n")
    return d


@needs_graphify
def test_search_finds_a_learnt_heading_and_reads_its_section(learnt):
    r = K.search("pdebug queue")
    hit = next(h for h in r["graph"] if "pdebug" in h["title"])
    assert hit["source"] == "capabilities/system_demo/SKILL.md"
    text = K.read(hit["source"], hit["line"])["text"]
    assert "Use -q pdebug." in text and "## Storage" not in text and "### other" not in text


@needs_graphify
def test_dftracer_skills_are_in_the_graph(learnt):
    if not K._dftracer_skills():
        pytest.skip("dftracer-agents is not installed")
    K.ensure()
    assert (K._root() / "stage/dftracer/system-tuolumne/SKILL.md").exists()
    assert not (K._root() / "stage/dftracer/phronix-memory").exists()


@needs_graphify
def test_the_graph_rebuilds_only_on_a_change(learnt):
    assert K.ensure()["rebuilt"]
    assert not K.ensure()["rebuilt"]
    assert K.remember("a-new-fact", "the scratch filesystem is purged weekly", "Purged on Sundays.", "project")["status"] == "ok"
    assert K.ensure()["rebuilt"]
    assert (K._root() / "stage/memory/a-new-fact.md").exists()


def test_body_text_is_found_without_the_graph(learnt, monkeypatch):
    monkeypatch.setattr(K, "_graphify", lambda: None)
    K.remember("purge-policy", "scratch rules", "The filesystem is purged on Sundays. See [[linked-fact]].", "project")
    K.remember("linked-fact", "another", "Unrelated words.", "project")
    r = K.search("purged Sundays")
    assert "graphify" in r["graph_error"] and r["graph"] == []
    assert [m["name"] for m in r["memory"]][:2] == ["purge-policy", "linked-fact"]


def test_their_memories_are_searched(learnt):
    if not K._dftracer_memory():
        pytest.skip("dftracer-agents is not installed")
    theirs = next(K._dftracer_memory().glob("*.md"))
    K.ensure()
    assert (K._root() / "stage/dftracer-memory" / theirs.name).exists()
    assert any(m["source"].startswith("dftracer-memory/") for m in K.search_memory(theirs.stem.replace("-", " ")))


def test_a_pack_shows_each_row_once_per_session(learnt, monkeypatch):
    monkeypatch.setattr(K, "_graphify", lambda: None)
    K.remember("purge-policy", "scratch rules", "Purged on Sundays.", "project")
    first = K.pack("purged Sundays", "s1")
    assert "<moirai-context>" in first and "purge-policy" in first
    assert K.pack("purged Sundays", "s1") == ""
    assert "purge-policy" in K.pack("purged Sundays", "s2")


def test_the_hook_adds_context_and_never_blocks_a_prompt(learnt):
    hook = Path(K.__file__).parents[1] / ".agents/hooks/context.py"
    K.remember("purge-policy", "scratch rules", "Purged on Sundays.", "project")
    env = {**os.environ, "PYTHONPATH": str(Path(K.__file__).parents[2])}
    run = lambda text: subprocess.run([sys.executable, str(hook)], input=text, capture_output=True, text=True, env=env, timeout=30)
    r = run(json.dumps({"prompt": "purged Sundays", "session_id": "x"}))
    assert r.returncode == 0 and "purge-policy" in r.stdout
    r = run("not json")
    assert r.returncode == 0 and r.stdout == ""


def test_a_memory_must_be_well_formed(learnt):
    assert K.remember("Bad Name", "d", "b", "project")["status"] == "error"
    assert K.remember("good-name", "d", "b", "guess")["status"] == "error"
    assert K.remember("good-name", "d", "no why", "feedback")["status"] == "error"
    assert K.remember("good-name", "d", "x\n**Why:** y\n**How to apply:** z", "feedback")["status"] == "ok"


def test_read_stays_in_the_graph(learnt, tmp_path):
    (K._root() / "stage").mkdir(parents=True)
    assert K.read("../../../../etc/passwd")["status"] == "error"


def lesson_on_demo(learnt, text):
    (learnt / "LESSONS.md").write_text(f"# Lessons: system_demo\n\n- [L-1234abcd] {text}\n\n- [L-5678abcd] something unrelated about zebras\n")


@needs_graphify
def test_a_lesson_is_found_through_the_graph_and_read_alone(learnt):
    lesson_on_demo(learnt, "uv venv fails on lustre from a compute node, build on the login node")
    hit = next(r for r in K.lessons("uv venv fails with No such file on lustre")["results"] if "source" in r)
    assert hit["source"] == "capabilities/system_demo/LESSONS.md" and "L-1234abcd" in hit["title"]
    text = K.read(hit["source"], hit["line"])["text"]
    assert "login node" in text and "zebras" not in text


@needs_graphify
def test_only_lesson_sections_of_a_skill_count(learnt):
    (learnt / "SKILL.md").write_text("---\nname: system_demo\n---\n\n## Scheduler\n\n### flux queue basics\nx\n\n## Lessons\n\n### flux queue hangs on pall\ny\n")
    titles = [r.get("title", "") for r in K.lessons("flux queue")["results"]]
    assert any("pall" in t for t in titles) and not any("basics" in t for t in titles)


def test_a_failure_carries_the_closest_lessons(learnt, monkeypatch):
    monkeypatch.setattr(K, "_graphify", lambda: None)
    L.add("lessons", "the moirai venv must never live on NFS", "user")
    r = K.on_failure("moirai_env_check", {"status": "error", "error": "moirai venv missing on NFS"})
    assert "NFS" in r["known_lessons"][0]["text"] and "before_exploring" in r
    r = K.on_failure("job_status", {"status": "done", "rc": 2, "log_tail": "moirai venv on NFS failed"})
    assert r["known_lessons"]


def test_success_and_the_normal_flow_carry_nothing(learnt, monkeypatch):
    monkeypatch.setattr(K, "_graphify", lambda: None)
    L.add("lessons", "the moirai venv must never live on NFS", "user")
    for tool, res in (("job_status", {"status": "done", "rc": 0}), ("explore_run", {"status": "ok"}), ("pipeline_advance", {"status": "error", "error": "moirai NFS"})):
        assert K.on_failure(tool, res) == res
