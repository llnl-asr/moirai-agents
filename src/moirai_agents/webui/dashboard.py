"""The live Dashboard, inside the web server: no service of its own.

One thread tails every trace set being watched and appends what is new to a :class:`Feed`; the Dashboard tab
holds a server-sent-events stream that pushes it. A running ``moirai-service`` needs no integration: its trace is the
interface. Finished traces go through the same path, read once. A feed nobody has watched for a while is dropped.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Dict

from ..core import provtrace as T

POLL = 0.5
KEEP = 20000                   # delta items held for reconnecting viewers
IDLE_DROP = 120.0


class Feed:
    def __init__(self, traces: str) -> None:
        self.traces = traces
        self.tail, self.graph = T.Tail(traces), T.Graph()
        self.items = collections.deque(maxlen=KEEP)
        self.seq = 0
        self.cond = threading.Condition()
        self.viewed = time.time()
        self.rate = 0.0

    def poll(self) -> None:
        got = self.graph.extend(self.tail.poll())
        with self.cond:
            for it in got:
                self.seq += 1
                self.items.append((self.seq, it))
            self.rate = 0.7 * self.rate + 0.3 * (len(got) / POLL)
            self.cond.notify_all()

    def since(self, seq: int) -> tuple:
        with self.cond:
            return [i for s, i in self.items if s > seq][:2000], self.seq

    def counts(self) -> dict:
        return {**self.graph.counts(), "rate": round(self.rate, 1), "trace_files": len(self.tail.pos)}


class Dashboards:
    def __init__(self) -> None:
        self.feeds: Dict[str, Feed] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="dashboard").start()

    def get(self, traces: str) -> Feed:
        with self.lock:
            f = self.feeds.get(traces)
            if f is None:
                f = self.feeds[traces] = Feed(traces)
                f.poll()
            f.viewed = time.time()
            return f

    def _run(self) -> None:
        while True:
            time.sleep(POLL)
            for key, f in list(self.feeds.items()):
                try:
                    f.poll()
                except Exception as e:
                    print(f"dashboard: {key}: {type(e).__name__}: {e}", flush=True)
                if time.time() - f.viewed > IDLE_DROP:
                    with self.lock:
                        self.feeds.pop(key, None)
