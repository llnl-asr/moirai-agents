"""History of a chat, from its provenance: what exactly happened, turn by turn.

Each turn: the message, mode, model, status, duration, cost, and every tool call (main agent and sub-agents) with its
input and result, in order. Compact by default (the page shows one line per turn, one per call); the full input and
result of a call are only sent when asked for, so a long chat stays light.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from . import runtime as R

CLIP = 400            # characters of a call's input / result in the overview
FULL = 60000          # characters of one call's input / result when it is opened


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) and "text" in c else c.get("content") if isinstance(c, dict) else c)
                         for c in content if c is not None)
    return "" if content is None else json.dumps(content, default=str)


def _short(name: str) -> str:
    return name.replace("mcp__moirai__", "").replace("mcp__", "")


def read(user: dict, session: dict, call: Optional[str] = None) -> dict:
    """{turns: [{turn_id, ts, message, mode, model, status, duration_s, cost_usd, steers, calls: [...]}]}; with
    call=<tool_use id>, that one call in full instead."""
    f = R.PROV.chat_file(user, session)
    turns: List[dict] = []
    by_turn: Dict[str, dict] = {}
    calls: Dict[str, dict] = {}
    try:
        fh = open(f)
    except OSError:
        return {"turns": [], "file": str(f), "note": "no provenance recorded for this chat yet"}
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            tid, kind = r.get("turn_id"), r.get("kind")
            t = by_turn.get(tid)
            if t is None and tid:
                t = by_turn[tid] = {"turn_id": tid, "ts": r.get("ts"), "message": "", "calls": [], "steers": [],
                                    "status": "running", "agents": 0}
                turns.append(t)
            if kind == "turn_start":
                t.update(message=r.get("message") or "", mode=r.get("mode"), model=r.get("model"), ts=r.get("ts"))
            elif kind == "turn_end":
                t.update(status=r.get("status"), duration_s=r.get("duration_s"), cost_usd=r.get("cost_usd"),
                         num_turns=r.get("num_turns"), end=r.get("ts"), errors=r.get("errors") or [])
            elif kind == "steer":
                t["steers"].append({"ts": r.get("ts"), "text": r.get("message")})
            elif kind == "context_reset":
                t["steers"].append({"ts": r.get("ts"), "text": f"(context reset: {r.get('reason')})"})
            elif kind == "stream" and t is not None:
                m = r.get("msg") or {}
                parent = m.get("parent_tool_use_id")
                content = (m.get("message") or {}).get("content")
                if m.get("type") == "assistant" and isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            inp = b.get("input") or {}
                            c = {"id": b.get("id"), "ts": r.get("ts"), "name": _short(str(b.get("name") or "")),
                                 "parent": parent, "desc": str(inp.get("description") or inp.get("command") or inp.get("file_path")
                                                               or inp.get("query") or inp.get("question") or inp.get("pattern") or "")[:200],
                                 "input": inp, "result": None, "is_error": False, "end": None}
                            if c["name"] in ("Task", "Agent"):
                                t["agents"] += 1
                            calls[c["id"]] = c
                            t["calls"].append(c)
                elif m.get("type") == "user" and isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                            c = calls[b["tool_use_id"]]
                            c.update(result=_text(b.get("content")), is_error=bool(b.get("is_error")), end=r.get("ts"))
    if call:
        c = calls.get(call)
        if not c:
            return {"error": "no such call in this chat"}
        inp = json.dumps(c["input"], indent=1, default=str)
        return {**{k: c[k] for k in ("id", "name", "ts", "end", "is_error", "parent", "desc")},
                "input": inp[:FULL], "input_cut": len(inp) > FULL,
                "result": (c["result"] or "")[:FULL], "result_cut": len(c["result"] or "") > FULL}
    for t in turns:            # the overview is one line per call: input and result come when a call is opened
        t["message"] = t["message"][:CLIP * 5]
        for c in t["calls"]:
            c.pop("input", None)
            c.pop("result", None)
            c["secs"] = round(c["end"] - c["ts"], 1) if c.get("end") and c.get("ts") else None
    return {"turns": turns, "file": str(f)}
