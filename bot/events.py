"""
Thread-safe event bus that bridges synchronous bot threads to the
async FastAPI/WebSocket event loop.

Bot threads call publish() freely; the bus uses loop.call_soon_threadsafe
to enqueue events into each connected WebSocket handler's asyncio.Queue.
History is kept so reconnecting clients can replay recent events.
"""

import asyncio
import threading
import time
from collections import deque
from typing import Optional


class EventBus:
    def __init__(self, history_size: int = 300):
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque = deque(maxlen=history_size)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the FastAPI startup handler with the running loop."""
        self._loop = loop

    def publish(self, event_type: str, data: dict) -> None:
        """Publish an event from any thread."""
        event = {"type": event_type, "data": data, "ts": time.time()}
        with self._lock:
            self._history.append(event)
            subs = list(self._subscribers)
        if self._loop and self._loop.is_running():
            for q in subs:
                self._loop.call_soon_threadsafe(q.put_nowait, event)

    def subscribe(self) -> asyncio.Queue:
        """Register a new WebSocket consumer. Returns its private queue."""
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def get_history(self) -> list:
        with self._lock:
            return list(self._history)
