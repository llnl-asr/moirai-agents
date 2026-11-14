"""A user message sent while a long moirai tool runs reaches the agent at once; the tool goes on (tool_wait)."""
import json
import threading
import time

import pytest

pytest.importorskip("phronix.engine.steer")
from phronix.engine import steer  # noqa: E402

from moirai_agents import mcp_server as M  # noqa: E402


def _call(name, **args):
    r = M.handle({"method": "tools/call", "params": {"name": name, "arguments": args}})
    return json.loads(r["content"][0]["text"])


def test_a_steer_returns_the_running_tool_at_once_and_tool_wait_gives_its_result(tmp_path, monkeypatch):
    ib = tmp_path / "chat.inbox"
    monkeypatch.setenv(steer.ENV, str(ib))
    monkeypatch.setattr(steer, "POLL_S", 0.02)
    monkeypatch.setattr(M, "refresh", lambda: False)
    steer.reset(ib)
    release = threading.Event()

    def pipeline():
        release.wait(10)
        return {"status": "ok", "answer": 42}
    monkeypatch.setitem(M.TOOLS, "zz_pipeline", {"fn": pipeline, "schema": {"name": "zz_pipeline"}})
    names = [t["name"] for t in M.handle({"method": "tools/list"})["tools"]]
    assert "tool_wait" in names and "tool_calls" in names
    out = {}
    th = threading.Thread(target=lambda: out.update(r=_call("zz_pipeline")))
    th.start()
    time.sleep(0.15)
    assert steer.wait_claimed(ib, steer.post(ib, "only the last month"), 2)
    th.join(2)
    r = out["r"]
    assert r["status"] == "still_running" and r["user_messages"] == ["only the last month"] and M._steer_busy()
    release.set()
    assert _call("tool_wait", call_id=r["call_id"], wait_s=5) == {"status": "ok", "answer": 42}
    assert not M._steer_busy()
