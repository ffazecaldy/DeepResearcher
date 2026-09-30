"""Typed in-process event bus with SQLite persistence; SSE consumers subscribe per run."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Callable

from app.models import Event


class EventBus:
    """Fan-out of typed events to per-run subscriber queues."""

    def __init__(self, persist: Callable[[Event], None] | None = None):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._persist = persist

    def publish(self, event: Event) -> None:
        """Persist (if a sink is configured) and fan out; never blocks the caller."""
        if self._persist:
            try:
                self._persist(event)
            except Exception:  # persistence must not break the pipeline
                pass
        for q in list(self._subs.get(event.run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self._subs.get(run_id, set()).discard(q)
