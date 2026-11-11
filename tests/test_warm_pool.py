"""The warm pool: a chat's next agent is started before the message, and a turn uses it only when it matches.

A fake `claude` stands in for Claude Code: it takes STARTUP seconds to "start its MCP servers", then answers each
stream-json message on stdin. A warm turn must not pay that start-up; a mismatched warm agent must not be used."""
import json
import sys
import textwrap
import threading
import time
import uuid

import pytest

from moirai_agents.webui import runtime as R
from moirai_agents.webui import warm as W

STARTUP = 3.0

FAKE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, sys, time
    if "--version" in sys.argv:
        print("2.0.0 (Claude Code)"); sys.exit(0)
    time.sleep({STARTUP})                       # Claude Code + MCP servers starting
    resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
    sid = resume or "sess-1"
    out = lambda d: (sys.stdout.write(json.dumps(d) + "\\n"), sys.stdout.flush())
    for line in sys.stdin:
        msg = json.loads(line)["message"]["content"]
        out({{"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": [], "tools": []}})
        out({{"type": "user", "message": {{"role": "user", "content": msg}}}})
        out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "echo " + msg}}]}}}})
        out({{"type": "result", "session_id": sid, "is_error": False, "result": "echo " + msg + (" resumed" if resume else "")}})
    """)


class FakeApp:
    def __init__(self, store):
        self.turns, self._store = {}, store

    def store(self, user):
        return self._store


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.write_text(FAKE)
    claude.chmod(0o755)
    monkeypatch.setattr(R, "claude_bin", lambda: str(claude))
    monkeypatch.setattr(R, "PROV", R.Provenance(tmp_path / "prov"))
    monkeypatch.setenv("MOIRAI_AGENTS_NO_LEARN_CHECK", "1")
    ws = tmp_path / "ws"
    (ws / ".moirai-agents").mkdir(parents=True)
    user = {"uid": "local", "name": "t", "workspace": str(ws), "owner": True, "provider": "anthropic",
            "key": "sk-ant-api03-test", "models": ["haiku", "sonnet"]}
    store = R.Store(ws / ".moirai-agents")
    app = FakeApp(store)
    pool = W.WarmPool(app)
    monkeypatch.setattr(R, "WARM", pool)
    yield app, pool, user, store
    pool.stop_all()


def chat(**kv):
    return {"id": uuid.uuid4().hex[:12], "turns": [], "mode": "ask", "model": "haiku", "app_dir": None, **kv}


def run(app, user, store, s, msg):
    t0 = time.time()
    t = R.Turn(s, msg, store, None, user)
    app.turns[f"{user['uid']}/{s['id']}"] = t
    t.start()
    with t.cond:
        t.cond.wait_for(lambda: t.done, timeout=30)
    res = next((e for e in t.events if e["type"] == "result"), {})
    return res.get("text"), time.time() - t0, t


def ready(pool, sid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if any(x["chat"] == sid for x in pool.status()) and not pool.pending:
            return True
        time.sleep(0.1)
    return False


def test_a_warm_agent_answers_without_the_start_up(env):
    app, pool, user, store = env
    s = chat()
    text, cold, t = run(app, user, store, s, "one")
    assert text == "echo one" and cold >= STARTUP and t.warm is None
    # the turn's end warmed the chat's next message, resuming the session it left
    assert ready(pool, s["id"])
    time.sleep(STARTUP + 0.5)                        # the user reads the answer meanwhile
    text, warm, t = run(app, user, store, s, "two")
    assert text == "echo two resumed"
    assert warm < STARTUP / 2, f"warm turn took {warm:.1f}s"


def test_a_message_while_the_agent_still_starts_uses_it(env):
    app, pool, user, store = env
    s = chat()
    store.put(s)
    pool.want(user, s)
    assert ready(pool, s["id"])
    text, secs, _ = run(app, user, store, s, "hi")    # sent right away: it waits for the rest of the start-up only
    assert text == "echo hi" and secs < STARTUP + 1.5


@pytest.mark.parametrize("change", [{"model": "sonnet"}, {"mode": "run"}, {"claude_session": "other"}])
def test_a_warm_agent_started_differently_is_not_used(env, change):
    app, pool, user, store = env
    s = chat()
    pool.want(user, s)
    assert ready(pool, s["id"])
    pid = pool.status()[0]["pid"]
    s.update(change)
    text, secs, t = run(app, user, store, s, "x")
    assert text and text.startswith("echo x") and t.proc.pid != pid and secs >= STARTUP
    assert not pool.status() or pool.status()[0]["pid"] != pid


def test_an_ended_turn_makes_older_agents_stale(env):
    app, pool, user, store = env
    a, b = chat(), chat()
    store.put(b)
    pool.want(user, b)
    assert ready(pool, b["id"])
    old = {x["chat"]: x["pid"] for x in pool.status()}[b["id"]]
    run(app, user, store, a, "learn something")        # may add capabilities/lessons
    end = time.time() + 15
    while time.time() < end and {x["chat"]: x["pid"] for x in pool.status()}.get(b["id"]) in (None, old):
        time.sleep(0.1)
    now = {x["chat"]: x["pid"] for x in pool.status()}
    assert now.get(b["id"]) and now[b["id"]] != old    # b was started again, after a's turn


def test_a_new_chat_takes_the_spare_agent(env):
    app, pool, user, store = env
    pool.want_fresh(user)
    sid = pool.fresh["local"]["id"]
    assert ready(pool, sid)
    assert pool.reserve(user, {"mode": "run", "model": "haiku"}) is None      # another mode: not this one
    s = chat(model="haiku")
    got = pool.reserve(user, s)
    assert got == sid and pool.reserve(user, s) is None                       # handed out once
    s["id"] = got
    time.sleep(STARTUP + 0.5)
    text, secs, _ = run(app, user, store, s, "new")
    assert text == "echo new" and secs < STARTUP / 2


def test_the_cap_drops_the_least_recently_wanted(env, monkeypatch):
    app, pool, user, store = env
    monkeypatch.setattr(W, "MAX", 2)
    chats = [chat() for _ in range(3)]
    for s in chats:
        pool.want(user, s)
        assert ready(pool, s["id"])
    assert {x["chat"] for x in pool.status()} == {chats[1]["id"], chats[2]["id"]}


def test_off_when_max_is_zero(env, monkeypatch):
    app, pool, user, store = env
    monkeypatch.setattr(W, "MAX", 0)
    s = chat()
    pool.want(user, s)
    time.sleep(0.3)
    assert pool.status() == [] and pool.take(user, s) is None
