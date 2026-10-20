"""A tiny thread-safe event bus feeding Server-Sent Events to the GUI."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 400):
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq, "ts": time.time()}
            # page images and full page dicts are large: keep history lean
            if event.get("type") not in ("page_done", "layout", "page_image"):
                self._history.append(event)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_put, queue, event)
            except RuntimeError:  # loop closed
                with self._lock:
                    self._subscribers.discard((loop, queue))

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.add((loop, queue))
        return loop, queue

    def unsubscribe(self, token) -> None:
        with self._lock:
            self._subscribers.discard(token)

    def recent(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        return [e for e in items if job_id is None or e.get("job_id") == job_id]


def _put(queue: asyncio.Queue, event: dict) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover
            pass
    queue.put_nowait(event)


def sse_format(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
