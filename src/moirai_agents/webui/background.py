"""Background agents: always-on runs that are not chats.

- learner: reads the chat turns that finished since its last run (their provenance) and turns what they met - new
  workloads, software, tools, systems, ways of doing things, and everything the user said - into lessons and
  capabilities. Complex procedures become one capability per step plus a parent.
- consolidation (lessons, capabilities): when consolidate.report() finds new overlap or conflict groups, merges them.

Both only PROPOSE: lessons and capabilities still wait for the user's approval in the right panel. Their runs live in
their own store (<workspace>/.moirai-agents/background), never in the chat list; the Agents panel shows them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import runtime as R
from ..core import consolidate, pipeline

LEARN_S = float(os.environ.get("MOIRAI_AGENTS_LEARN_S", "600"))              # how often the learner looks for new turns
LEARN_BATCH = int(os.environ.get("MOIRAI_AGENTS_LEARN_BATCH", "12"))         # finished turns per learner run
CONSOL_GAP_H = float(os.environ.get("MOIRAI_AGENTS_CONSOLIDATION_GAP_H", "6"))  # automatic consolidation at most this often
KEEP = 20                                                                    # recent background runs listed

BG_RULES = ("You are a BACKGROUND run, not a chat: nobody reads your turn live and nobody can answer. Never call ask_user. "
            "Work on this node only: no jobs, no compute nodes, no builds. Everything you write is a proposal the user "
            "approves in the right panel. End with a short plain report: what you proposed (ids and names), and what you "
            "left for the user to decide and why.\n\n")

LEARN_ASK = (
    "You are the LEARNER. Below are chat turns that finished since your last run. For each, read what happened - the "
    "provenance file has every message and tool call (Read it in parts; the 'stream' records hold tool_use and "
    "tool_result blocks). Then capture, before anything else:\n"
    "1. Every new or different workload, software, tool, system, or way of doing something the turn met: "
    "knowledge_search, then capability_update the capability that covers it, else capability_new "
    "(workload_<x>, software_<x>, tools_<x>, system_<x>), structured as capability_builder says.\n"
    "2. A complex procedure becomes one capability per step, each with its own tool and checks, plus a parent whose "
    "skill lists the steps in order (Cross-references). Reuse a step that exists.\n"
    "3. Everything the user said that teaches something - a correction, a preference, a choice at a gate, a rejection, "
    "a condition, a repeated request - becomes lesson_add on the capability it concerns, quoting the user as evidence.\n"
    "4. Skip what a lesson or capability already says (lesson_search first), chit-chat, and facts only that chat needs.\n"
    "Turns:\n")

CONSOL_ASK = {
    "lessons": ("Consolidate the lessons. Call consolidation_report and read the lessons groups. For overlapping lessons, "
                "propose one merged lesson with lesson_merge. Lessons that contradict each other: do not pick one; list "
                "them in your report for the user. A group that is different on purpose: consolidation_keep."),
    "capabilities": ("Consolidate the capabilities. Call consolidation_report and read the capabilities groups, then each "
                     "capability with capability_get. For an overlap, propose moving what is shared to the better home "
                     "with capability_update; never disable one. Conflicting rules: do not pick one; list them in your "
                     "report for the user. A group that is different on purpose: consolidation_keep."),
}


class Background:
    """The learner and the two consolidation agents of one server."""

    def __init__(self, app: "R.App") -> None:
        self.app = app
        self.f = app.state / "background.json"
        try:
            self.st = json.loads(self.f.read_text())
        except (OSError, ValueError):
            self.st = {}
        self.st.setdefault("runs", [])
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- store + runs
    def store(self, user: dict) -> "R.Store":
        key = "bg:" + user["workspace"]
        if key not in self.app.stores:
            self.app.stores[key] = R.Store(Path(user["workspace"]) / ".moirai-agents" / "background")
        return self.app.stores[key]

    def _save(self) -> None:
        tmp = self.f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        os.replace(tmp, self.f)

    def _running(self, agent: str) -> Optional[dict]:
        for r in reversed(self.st["runs"]):
            if r["agent"] == agent:
                t = self.app.turns.get(r["key"])
                return r if t is not None and not t.done else None
        return None

    def start(self, agent: str, title: str, ask: str, **extra) -> dict:
        user = self.app.local_user()
        if not user.get("provider"):
            return {"error": "no model access configured (Settings → Model access)"}
        if self._running(agent):
            return {"error": f"the {agent} agent is already running"}
        st = self.store(user)
        sid = uuid.uuid4().hex[:12]
        allowed = user.get("models") or R.provider_models(user.get("provider", "livai"))
        model = os.environ.get("MOIRAI_AGENTS_BACKGROUND_MODEL") or os.environ.get("MOIRAI_AGENTS_MODEL", R.MODELS[0])
        s = {"id": sid, "created": R._now(), "turns": [], "user": user["name"], "mode": "ask", "app_dir": None,
             "background": agent, "title": title, "model": model if model in allowed else allowed[0]}
        pipeline.set_auto_mode(sid, True, user["workspace"])        # any question answers itself
        t = R.Turn(s, BG_RULES + ask, st, self.app.state, user)
        key = f"bg/{sid}"
        self.app.turns[key] = t
        st.put(s)
        t.start()
        with self.lock:
            self.st["runs"] = (self.st["runs"] + [{"agent": agent, "session": sid, "key": key, "title": title,
                                                   "at": time.time(), **extra}])[-KEEP:]
            self._save()
        R.wlog("background-start", agent=agent, session=sid, title=title)
        return {"status": "started", "session": sid, "agent": agent}

    # ---------------------------------------------------------------- learner
    def _new_turns(self) -> List[dict]:
        """Finished chat turns (provenance index) after the learner's mark, background runs excluded."""
        mark = self.st.get("learn_mark", time.time() - 86400)
        out = []
        try:
            with open(R.PROV.root / "index.jsonl") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("kind") == "turn" and r.get("ts", 0) > mark and not r.get("background"):
                        out.append(r)
        except OSError:
            pass
        bg = {x["session"] for x in self.st["runs"]}
        return [r for r in out if r.get("session") not in bg]

    def learn_once(self, force: bool = False) -> dict:
        turns = self._new_turns()
        if not turns:
            return {"status": "nothing new"}
        if self._running("learner"):
            return {"status": "running"}
        if not force and time.time() - self.st.get("learn_at", 0) < LEARN_S:
            return {"status": "waiting"}
        batch = turns[:LEARN_BATCH]
        lines = []
        for r in batch:
            lines.append(f"- chat {r.get('session')} \"{(r.get('title') or '')[:80]}\" ({r.get('mode')}, {r.get('status')}, "
                         f"{sum((r.get('tools') or {}).values())} tool calls)\n  user: {(r.get('message') or '')[:600]}\n"
                         f"  answer: {(r.get('answer') or '')[:600]}\n  provenance: {R.PROV.root / r.get('file', '')}")
        res = self.start("learner", f"Learner: {len(batch)} turn{'s' if len(batch) != 1 else ''}", LEARN_ASK + "\n".join(lines),
                         turns=len(batch))
        if res.get("status") == "started":
            with self.lock:
                self.st["learn_mark"] = batch[-1].get("ts", time.time())
                self.st["learn_at"] = time.time()
                self._save()
        return res

    # ---------------------------------------------------------------- consolidation
    @staticmethod
    def _fp(rep: dict, part: str) -> str:
        ids = sorted(",".join(sorted(g["ids"])) for g in rep.get(part) or [])
        return hashlib.sha1("|".join(ids).encode()).hexdigest() if ids else ""

    def consolidate_once(self, rep: dict, force: str = "") -> dict:
        out = {}
        for part in ("lessons", "capabilities"):
            fp, agent = self._fp(rep, part), f"consolidation-{part}"
            forced = force in (part, "all")
            last = self.st.get(agent, {})
            if not fp or (fp == last.get("fp") and not forced):
                continue
            if self._running(agent):
                out[part] = "running"
                continue
            if not forced and time.time() - last.get("at", 0) < CONSOL_GAP_H * 3600:
                out[part] = "waiting"
                continue
            n = len(rep[part])
            r = self.start(agent, f"Consolidation: {part} ({n} group{'s' if n != 1 else ''})", CONSOL_ASK[part], groups=n)
            if r.get("status") == "started":
                with self.lock:
                    self.st[agent] = {"fp": fp, "at": time.time()}
                    self._save()
            out[part] = r
        return out

    # ---------------------------------------------------------------- what the page shows
    def _live(self, key: str) -> dict:
        t = self.app.turns.get(key or "")
        if t is None:
            return {"running": False}
        evs = list(t.events)
        uses = [e for e in evs if e.get("type") == "tool_use"]
        last = (uses[-1].get("summary") or uses[-1].get("name") or "") if uses else ""
        res = next((e for e in reversed(evs) if e.get("type") in ("result", "error")), None)
        failed = t.done and (res is None or res.get("type") == "error" or bool(res.get("is_error")))
        return {"running": not t.done, "failed": failed, "tools": len(uses),
                "last": str(last).replace("mcp__moirai__", "")[:140], "started": t.started}

    def agents(self) -> List[dict]:
        out = []
        rep = getattr(self.app, "consolidation", None) or {}
        descs = {"learner": "Reads every finished chat turn and turns what it met and what you said into lessons and "
                            f"capabilities (complex ones split into step capabilities); checks every {LEARN_S / 60:g} min.",
                 "consolidation-lessons": "Merges overlapping lessons into one copy of each fact; conflicts are left to you.",
                 "consolidation-capabilities": "Moves what capabilities share to one home; conflicts are left to you."}
        names = {"learner": "learner", "consolidation-lessons": "lessons consolidation",
                 "consolidation-capabilities": "capabilities consolidation"}
        for agent in ("learner", "consolidation-lessons", "consolidation-capabilities"):
            r = next((x for x in reversed(self.st["runs"]) if x["agent"] == agent), None)
            live = self._live(r["key"]) if r else {"running": False}
            if r and r["key"] not in self.app.turns:
                saved = self.run(r["session"])
                live = {"running": False, "failed": bool(saved.get("failed")), "tools": saved.get("n_steps", 0)}
            if agent == "learner":
                pend = len(self._new_turns())
                idle = f"{pend} finished turn{'s' if pend != 1 else ''} to read" if pend else "waiting for finished turns"
            else:
                g = len(rep.get(agent.split("-", 1)[1]) or [])
                idle = f"{g} open group{'s' if g != 1 else ''}"
            if r:
                idle += f" · last run {time.strftime('%F %H:%M', time.localtime(r['at']))}"
            out.append({"id": agent, "name": names[agent], "kind": "background agent",
                        "status": "running" if live["running"] else ("failed" if live.get("failed") else "idle"),
                        "desc": descs[agent],
                        "last": ("→ " + live["last"]) if live["running"] and live.get("last") else
                                ("last run failed - click for what happened" if live.get("failed") else idle),
                        "tools": live.get("tools", 0), "started": live.get("started") or (r or {}).get("at"),
                        "chat": r["session"] if r else None})
        return out

    def run(self, sid: str) -> dict:
        """What one background run did: its tool steps and its final report."""
        if not re.fullmatch(r"[0-9a-f]{6,32}", sid or ""):
            return {"error": "bad id"}
        t = self.app.turns.get(f"bg/{sid}")
        s = t.snapshot() if t is not None and not t.done else self.store(self.app.local_user()).get(sid)
        if not s:
            return {"error": "not found"}
        turns = [x for x in s.get("turns") or [] if x.get("role") == "assistant"]
        evs = (t.events if t is not None and not t.done else (turns[-1].get("events") if turns else None)) or []
        steps = []
        for e in evs:
            if e.get("type") == "tool_use":
                steps.append({"tool": str(e.get("name", "")).replace("mcp__moirai__", "").replace("mcp__", ""),
                              "summary": (e.get("summary") or str(e.get("input") or ""))[:200]})
            elif e.get("type") == "tool_result" and steps and e.get("is_error"):
                steps[-1]["error"] = True
        text = "".join(e.get("text", "") for e in evs if e.get("type") == "text") or (turns[-1].get("text", "") if turns else "")
        running = bool(s.get("running"))
        res = next((e for e in reversed(evs) if e.get("type") in ("result", "error")), None)
        failed = not running and (res is None or res.get("type") == "error" or bool(res.get("is_error")))
        return {"id": sid, "title": s.get("title"), "kind": s.get("background"), "running": running, "failed": failed,
                "failure": str(((res or {}).get("text") if failed else "") or ("it ended without a result" if failed else ""))[:600],
                "steps": steps[-60:], "n_steps": len(steps), "report": text[-4000:]}


def watch(app: "R.App") -> None:
    """The server's loop: start the learner and consolidation when there is something for them."""
    bg = app.background
    while True:
        try:
            if os.environ.get("MOIRAI_AGENTS_AUTO_LEARN", "1") != "0":
                bg.learn_once()
            if os.environ.get("MOIRAI_AGENTS_AUTO_CONSOLIDATE", "1") != "0" and getattr(app, "consolidation", None):
                bg.consolidate_once(app.consolidation)
        except Exception as e:  # noqa: BLE001  never end the loop
            print(f"{time.strftime('%F %T')} background check failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(60)
