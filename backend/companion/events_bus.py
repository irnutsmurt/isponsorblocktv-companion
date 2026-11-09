"""In-process pub/sub for live UI updates (SSE). Bounded per-subscriber queues; slow clients drop."""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.seq = 0
        self.dropped = 0

    def subscribe(self, maxsize: int = 200) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, type_: str, data: Any = None) -> None:
        self.seq += 1
        msg = {"seq": self.seq, "type": type_, "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.dropped += 1
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass

    @property
    def subscribers(self) -> int:
        return len(self._subs)
