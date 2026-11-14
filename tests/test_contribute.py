"""moirai-agents in the capability chain: upstream capabilities load read-only; what it learns about one of them is
committed in that upstream's clone on its integration branch, for a person to push (on a new branch) and open a PR."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def chain(tmp_path, monkeypatch):
    from phronix.engine import config, configure
    from phronix.engine.scopes import Scope
    from moirai_agents.core import _engine
    origin = tmp_path / "up.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True, capture_output=True)
    c = seed / "src" / "uppkg" / ".agents" / "capabilities" / "workload_x"
    c.mkdir(parents=True)
    (c / "capability.json").write_text(json.dumps({"name": "workload_x", "version": 1, "enabled": True, "tools": []}))
    (c / "tool.py").write_text("def register(mcp):\n    pass\n")
    (c / "LESSONS.md").write_text("# Lessons: workload_x\n")
    git(seed, "config", "user.email", "t@example.org"); git(seed, "config", "user.name", "t")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "init"); git(seed, "push", "-q", "origin", "HEAD:main")
    proj = tmp_path / "proj"
    clone = proj / "vendor" / "up"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "t@example.org"); git(clone, "config", "user.name", "t")
    own = tmp_path / "own"
    own.mkdir()
    # Not a test store (that never contributes): the project's own store is a temp dir, the engine is moirai's.
    for k in ("PROJECT_CAPABILITIES", "USER_CAPABILITIES", "BUNDLED_CAPABILITIES"):
        monkeypatch.delenv(f"MOIRAI_AGENTS_{k}", raising=False)
    up = Scope("up", "uppkg", clone / "src" / "uppkg" / ".agents" / "capabilities", root=True)
    configure(project="moirai-agents", env_prefix="MOIRAI_AGENTS", package_dir=_engine.PKG, scope=_engine.scope(),
              upstream=[up], project_root=proj, store=own)
    yield {"clone": clone, "proj": proj, "own": own}
    configure(project="moirai-agents", env_prefix="MOIRAI_AGENTS", package_dir=_engine.PKG, scope=_engine.scope(),
              upstream=_engine.upstream(), bootstrap="moirai_agents.core._engine")


def test_a_lesson_on_an_upstream_capability_is_committed_upstream(chain):
    from moirai_agents.core import learning
    r = learning.add("workload_x", "prefer the small input for smoke runs", source="user")
    assert r["status"] == "applied" and r["contributed"][0]["branch"] == "main", r
    clone = chain["clone"]
    assert "small input" in (clone / "src/uppkg/.agents/capabilities/workload_x/LESSONS.md").read_text()
    assert git(clone, "status", "--porcelain") == "" and git(clone, "branch", "--show-current") == "main"


def test_the_repositories_panel_pulls_pushes_on_a_new_branch_and_switches_back(chain):
    from moirai_agents.core import learning
    from moirai_agents.webui import api
    learning.add("workload_x", "one more", source="user")
    rows = {r["project"]: r for r in api._repos()["repos"]}
    assert rows["up"]["contribution"]["ahead"] == 1 and rows["up"]["actions"] == ["pull", "push"]
    assert api._repos("rebase", "up")["upstream"][0]["rebase"] in ("ok", "up_to_date")
    p = api._repos("push", "up")["upstream"][0]
    assert p["pushed"] and p["pushed_branch"].startswith("contrib/moirai-agents-"), p
    assert "switch" in {r["project"]: r for r in api._repos()["repos"]}["up"]["actions"]
    s = api._repos("switch", "up")
    assert s["status"] == "ok" and git(chain["clone"], "rev-parse", "HEAD") == git(chain["clone"], "rev-parse", "origin/main")
