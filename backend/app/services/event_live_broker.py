"""Process-local wakeups for the durable event log (ADR §3.10).

Each API process owns one PostgreSQL ``LISTEN`` connection, regardless of how
many sockets are connected. The listener runs on its own thread, so neither the
connection nor PostgreSQL I/O can occupy the event loop. A notification carries
nothing but a hint: the socket wakes, reads the committed rows after its own
cursor in a worker thread, and applies the caller's policy and folder
visibility there. Coalescing hints is safe for the same reason — a full queue
already means "read the log".
"""

from __future__ import annotations

import asyncio
import logging
import threading

from app.services.event_log import CHANNEL
from app.services.postgres_database import connection_kwargs, postgres_dsn


logger = logging.getLogger(__name__)

# How long one LISTEN read waits before the thread checks for shutdown. Short
# enough that stop() returns promptly, long enough to cost no measurable idle.
_LISTEN_TIMEOUT_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0


class EventLiveBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._listener_reconnects = 0

    def metrics_snapshot(self) -> dict[str, float | int | bool]:
        return {
            "connections": len(self._subscribers),
            "listenerConnected": self.ready.is_set(),
            "listenerReconnects": self._listener_reconnects,
        }

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    def wake(self) -> None:
        for queue in tuple(self._subscribers):
            if not queue.full():
                queue.put_nowait(None)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self.ready.clear()
        self._thread = threading.Thread(
            target=self._listen, name="event-live-listener", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            # The LISTEN read wakes at least once a second, so the join is short.
            await asyncio.to_thread(thread.join, _LISTEN_TIMEOUT_SECONDS * 5)
        self._subscribers.clear()
        self._loop = None
        self.ready.clear()

    def _wake_from_listener(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.wake)
        except RuntimeError:
            # The loop closed between the check and the call; nobody is listening.
            pass

    def _listen(self) -> None:
        import psycopg

        delay = 1.0
        connected_once = False
        while not self._stop.is_set():
            try:
                dsn = postgres_dsn()
                kwargs = connection_kwargs(dsn)
                kwargs["autocommit"] = True
                with psycopg.connect(dsn, **kwargs) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    if connected_once:
                        self._listener_reconnects += 1
                    connected_once = True
                    delay = 1.0
                    self.ready.set()
                    while not self._stop.is_set():
                        # Ends after the timeout with no notification, which is
                        # when the loop re-checks for shutdown.
                        for _notification in conn.notifies(timeout=_LISTEN_TIMEOUT_SECONDS):
                            self._wake_from_listener()
            except Exception:
                if self._stop.is_set():
                    break
                logger.exception(
                    "Event change listener disconnected; sockets still replay the durable log"
                )
                self.ready.clear()
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)
        self.ready.clear()


broker = EventLiveBroker()
