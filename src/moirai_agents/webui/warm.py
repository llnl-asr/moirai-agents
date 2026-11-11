"""Warm agents: a chat's next `claude -p` process, started before the user sends the message.

A cold turn pays for the capability sync (seconds) and for Claude Code's start-up with its MCP servers (moirai, dftracer:
often 10-20 s) before the agent reads the message. `claude -p --input-format stream-json` does all of that, then waits on
stdin for the first message; so the pool starts that exact process ahead of time and the turn only writes the message.

What gets warmed (at most MOIRAI_AGENTS_WARM_MAX agents per server, the least recently wanted is dropped first):
  - a chat that is opened in the UI (its session page, the Query view's chat of a pipeline),
  - the chat whose turn just ended (its next message resumes the session the turn left),
  - one spare new chat per user (mode ask, the default model), handed out by reserve() to a new chat.

A warm agent is used only when its command, environment and folder match what the turn would start now (spec_signature:
model, mode prompt + approved lessons, --resume id, credentials) and no turn of this user ended after it started (a turn can
add capabilities or lessons); otherwise it is killed and the turn starts cold, as before. MOIRAI_AGENTS_WARM_MAX=0 turns it
off; MOIRAI_AGENTS_WARM_TTL (s) is how long an unused agent idles. Every agent carries the server's instance tag, so the
server's shutdown sweep finds any that are left.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Dict, Optional, Tuple

from . import runtime as R

MAX = int(os.environ.get("MOIRAI_AGENTS_WARM_MAX", "3"))
TTL = float(os.environ.get("MOIRAI_AGENTS_WARM_TTL", "1800"))
WAIT = float(os.environ.get("MOIRAI_AGENTS_WARM_WAIT", "15"))   # a message waits this long for an agent still being started
REAP = 30.0

Key = Tuple[str, str]   # (user uid, chat id)


class Warm:
    """One idle agent, waiting on stdin for its first message."""

    def __init__(self, sid: str, spec: dict, sig: str, proc: subprocess.Popen, started: float) -> None:
        self.sid, self.spec, self.sig, self.proc = sid, spec, sig, proc
        self.started = started          # when its spec was taken (before the sync): older than a turn's end = stale
        self.touched = time.time()
        self.err: list = []             # its stderr, drained while it idles (the turn reads it on failure)
        threading.Thread(target=lambda: self.err.extend(proc.stderr), daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        p = self.proc
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            return

        def reap():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
        threading.Thread(target=reap, daemon=True).start()


class WarmPool:
    def __init__(self, app) -> None:
        self.app = app
        self.lock = threading.Lock()
        self.entries: Dict[Key, Warm] = {}
        self.pending: Dict[Key, threading.Event] = {}     # being started (the sync runs first)
        self.epoch: Dict[str, float] = {}                  # uid -> when a turn of it last ended
        self.fresh: Dict[str, dict] = {}                   # uid -> the spare new chat's session
        threading.Thread(target=self._reaper, daemon=True).start()

    # ------------------------------------------------------------ policy
    @staticmethod
    def enabled(user: dict) -> bool:
        # OpenAI goes through a per-user bridge started inside the turn: not warmed
        return MAX > 0 and bool(user.get("provider")) and user.get("provider") != "openai"

    def _running(self, user: dict, sid: str) -> bool:
        t = self.app.turns.get(f"{user['uid']}/{sid}")
        return t is not None and not t.done

    def _stale(self, uid: str, w: Warm) -> bool:
        return w.started < self.epoch.get(uid, 0) or time.time() - w.started > TTL or not w.alive()

    # ------------------------------------------------------------ warming
    def want(self, user: dict, session: dict) -> None:
        """Have an agent ready for this chat's next message (cheap, idempotent: call it whenever the chat is shown)."""
        if not session or not self.enabled(user) or session.get("background") or self._running(user, session["id"]):
            return
        key = (user["uid"], session["id"])
        with self.lock:
            w = self.entries.get(key)
            if w is not None and not self._stale(user["uid"], w):
                w.touched = time.time()
                return
            if key in self.pending:
                return
            self.pending[key] = threading.Event()
        snap = {k: v for k, v in session.items() if k != "turns"}     # the spec needs no turns
        threading.Thread(target=self._spawn, args=(user, snap, key), daemon=True).start()

    def want_fresh(self, user: dict) -> None:
        """Keep one new chat ready (mode ask, the user's default model)."""
        if not self.enabled(user):
            return
        with self.lock:
            s = self.fresh.get(user["uid"])
            if s is None:
                allowed = user.get("models") or R.provider_models(user.get("provider", "livai"))
                model = os.environ.get("MOIRAI_AGENTS_MODEL") or R.MODELS[0]
                s = self.fresh[user["uid"]] = {"id": uuid.uuid4().hex[:12], "turns": [], "mode": "ask",
                                               "model": model if model in allowed else allowed[0], "app_dir": None}
        self.want(user, s)

    def reserve(self, user: dict, session: dict) -> Optional[str]:
        """A new chat about to start: the id of the spare one if it was warmed for the same mode and model."""
        with self.lock:
            s = self.fresh.get(user["uid"])
            key = (user["uid"], s["id"]) if s else None
            if (s is None or s["mode"] != session.get("mode", "ask") or s["model"] != session.get("model")
                    or (key not in self.entries and key not in self.pending)):
                return None
            del self.fresh[user["uid"]]
            return s["id"]

    def _spawn(self, user: dict, session: dict, key: Key) -> None:
        t0 = time.time()
        try:
            spec = R.turn_spec(session, user)          # the slow capability sync happens here, not in the turn
            if self._running(user, key[1]):             # the message came first and started cold
                return
            proc = subprocess.Popen(spec["cmd"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    bufsize=1, start_new_session=True)
            w = Warm(key[1], spec, R.spec_signature(spec), proc, t0)
            drop = []
            with self.lock:
                old = self.entries.pop(key, None)
                old and drop.append(old)
                self.entries[key] = w
                while len(self.entries) > MAX:              # least recently wanted first
                    k = min(self.entries, key=lambda k: self.entries[k].touched)
                    drop.append(self.entries.pop(k))
            for d in drop:
                d.kill()
            R.wlog("warm-start", chat=key[1], pid=proc.pid, secs=round(time.time() - t0, 1), pool=len(self.entries))
        except Exception as e:  # noqa: BLE001  warming is best effort: the turn starts cold
            R.wlog("warm-failed", chat=key[1], error=f"{type(e).__name__}: {e}"[:300])
        finally:
            with self.lock:
                ev = self.pending.pop(key, None)
            ev and ev.set()

    # ------------------------------------------------------------ use
    def take(self, user: dict, session: dict) -> Optional[Warm]:
        """The warm agent for this chat's turn, if it was started exactly as the turn would start one now."""
        if not self.enabled(user):
            return None
        key = (user["uid"], session["id"])
        with self.lock:
            ev = self.pending.get(key)
        if ev is not None:                  # being started: further along than a cold start would be
            ev.wait(timeout=WAIT)
        with self.lock:
            w = self.entries.pop(key, None)
        if w is None:
            return None
        why = None
        if self._stale(user["uid"], w):
            why = "stale"
        else:
            try:
                if w.sig != R.spec_signature(R.turn_spec(session, user, sync_caps=False)):
                    why = "changed"         # another model, mode, pipeline, resume id, lessons or credentials
            except Exception as e:  # noqa: BLE001
                why = f"spec: {e}"
        if why:
            R.wlog("warm-unused", chat=key[1], why=why)
            w.kill()
            return None
        return w

    def turn_ended(self, user: dict, session: dict) -> None:
        """A turn can add capabilities and lessons: every agent of this user started before now is stale. Re-warm them
        (the chats are still shown), and this chat's next message."""
        uid = user["uid"]
        with self.lock:
            self.epoch[uid] = time.time()
            again = [k[1] for k in self.entries if k[0] == uid and k[1] != session["id"]]
        st = self.app.store(user)
        for sid in again:
            s = st.get(sid) or (self.fresh.get(uid) if (self.fresh.get(uid) or {}).get("id") == sid else None)
            if s:
                self.want(user, s)
        self.want(user, session)
        self.want_fresh(user)

    # ------------------------------------------------------------ housekeeping
    def _reaper(self) -> None:
        while True:
            time.sleep(REAP)
            drop = []
            with self.lock:
                for k, w in list(self.entries.items()):
                    if time.time() - w.started > TTL or not w.alive():
                        drop.append(self.entries.pop(k))
                        if (self.fresh.get(k[0]) or {}).get("id") == k[1]:
                            del self.fresh[k[0]]
            for w in drop:
                w.kill()

    def stop_all(self) -> None:
        with self.lock:
            drop, self.entries = list(self.entries.values()), {}
        for w in drop:
            w.kill()

    def status(self) -> list:
        with self.lock:
            return [{"chat": k[1], "pid": w.proc.pid, "age_s": round(time.time() - w.started), "alive": w.alive()}
                    for k, w in self.entries.items()]
