"""A run pipeline's work lives in a real dftracer session. Skipped where dftracer-agents is not installed."""
import json
import os
from pathlib import Path

import pytest
from conftest import tools

from moirai_agents.core import dfbridge as B
from moirai_agents.core import pipeline as P


def test_sessions_ignore_an_exported_root(monkeypatch, tmp_path):
    monkeypatch.setenv("DFTRACER_WORKSPACES", "/some/other/project/workspaces")
    assert B.workspaces_root() == str(tmp_path / "uw" / "workspaces")


def test_a_pipeline_without_a_session_works_in_its_own_folder(tmp_path):
    c = P.new("run", "app", str(tmp_path), "true")
    assert P.work(c) == P.pdir(c["id"]) and P.source_of(c) == str(tmp_path)


def test_a_pipeline_with_a_session_works_in_it(tmp_path):
    c = P.set_session(P.new("run", "app", str(tmp_path), "true")["id"], {"run_id": "app/1", "workspace": str(tmp_path / "s")})
    assert P.work(c) == tmp_path / "s" / "provenance" and P.source_of(c) == str(tmp_path / "s" / "source")
    assert P.summary(c)["session"]["run_id"] == "app/1"


def test_real_session_end_to_end(tmp_path, monkeypatch):
    monkeypatch.delenv("MOIRAI_AGENTS_NO_SESSIONS")
    if not B.available():
        pytest.skip("dftracer-agents is not installed")
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text('def make(path):\n    with open(path, "w") as f:\n        f.write("abc")\n    return path\n\n\nif __name__ == "__main__":\n    make("out.txt")\n')
    t = tools()
    call = lambda n, **k: t[n]["fn"](**k)  # noqa: E731
    p = call("pipeline_new", kind="run", name="demo", source=str(app), command="python3 app.py")
    assert "session_error" not in p and p["session"]["workspace"].startswith(str(tmp_path / "uw" / "workspaces"))
    ws = Path(p["session"]["workspace"])
    assert (ws / "source" / "app.py").exists() and (ws / "session.json").exists()
    assert call("explore_run", pipeline=p["id"])["app"]["entry_points"] == ["app.py"]
    r = call("smoke_session", pipeline=p["id"], build=False)
    assert r["status"] == "ok", r
    assert "out.txt" in r["outputs_created"] and not (app / "out.txt").exists()      # the user's tree is untouched
    assert {h["function"] for h in call("discover_scan", pipeline=p["id"])["hits"]} >= {"make"}
    spec = {"scientific_artifacts": ["o"], "entity_types": [{"type": "o", "role": "output", "description": "the file the app writes", "id_scheme": "path"}],
            "activities": [{"file": "app.py", "function": "make", "name": "make", "generated": [{"type": "o", "key_expr": "path"}]}]}
    assert call("annotate_spec", pipeline=p["id"], spec=spec)["status"] == "ok"
    a = call("annotate_apply", pipeline=p["id"])
    assert a["annotated"] == str(ws / "provenance" / "source") and "_moirai_prov" in (ws / "provenance/source/app.py").read_text()
    assert json.loads((ws / "provenance/spec.json").read_text())["scientific_artifacts"] == ["o"]
    assert os.path.isdir(ws / "baseline")                                            # dftracer's own folders are still there
