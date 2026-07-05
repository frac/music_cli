"""A debounced, concurrent background download queue.

This is the heart of the ``browse`` UX: selecting a track schedules a download
that only starts after a short *debounce* window, so a track picked and unpicked
by accident is never transferred. The kid keeps browsing while a small pool of
workers copies the confirmed selections.

The queue is intentionally generic — it knows nothing about HTTP or catalogs.
Each item carries an opaque ``payload`` handed to an injected async
``downloader`` coroutine, which keeps this module unit-testable with a fake
clock and a fake downloader.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum


class DownloadState(StrEnum):
    """Lifecycle states reported through the event callback."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DownloadItem:
    """A unit of work: a unique ``key`` plus an opaque ``payload``."""

    key: str
    payload: object = None


Downloader = Callable[[DownloadItem], Awaitable[None]]
EventHook = Callable[[str, DownloadState], None]


@dataclass(slots=True)
class DownloadQueue:
    """Schedule and run debounced background downloads.

    Args:
        downloader: Coroutine that performs one download; may raise to signal
            failure (which triggers a retry).
        debounce: Seconds a selection must persist before its download starts.
        concurrency: Maximum simultaneous in-flight downloads.
        max_attempts: Attempts per item before it is marked failed.
        on_event: Optional callback invoked on every state transition.
        clock: Monotonic time source (injectable for tests).
    """

    downloader: Downloader
    debounce: float = 5.0
    concurrency: int = 3
    max_attempts: int = 3
    on_event: EventHook | None = None
    clock: Callable[[], float] = time.monotonic

    _pending: dict[str, float] = field(default_factory=dict, init=False)
    _items: dict[str, DownloadItem] = field(default_factory=dict, init=False)
    _inflight: set[str] = field(default_factory=set, init=False)
    _done: set[str] = field(default_factory=set, init=False)
    _failed: set[str] = field(default_factory=set, init=False)
    _running: bool = field(default=False, init=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _sem: asyncio.Semaphore | None = field(default=None, init=False)

    # -- selection API (called from the UI) --------------------------------

    def select(self, item: DownloadItem) -> None:
        """Schedule ``item`` for download after the debounce window."""
        key = item.key
        if key in self._done or key in self._inflight or key in self._pending:
            return
        self._failed.discard(key)  # allow re-selecting a previously failed item
        self._items[key] = item
        self._pending[key] = self.clock() + self.debounce
        self._emit(key, DownloadState.PENDING)
        self._wake()

    def deselect(self, key: str) -> None:
        """Cancel a still-pending download (no effect once it has started)."""
        if self._pending.pop(key, None) is not None:
            self._emit(key, DownloadState.CANCELLED)
            self._wake()

    def due_keys(self, now: float) -> list[str]:
        """Return pending keys whose debounce window has elapsed by ``now``."""
        return [key for key, deadline in self._pending.items() if deadline <= now]

    def counts(self) -> dict[str, int]:
        """Return current totals per state, for progress display."""
        return {
            "pending": len(self._pending),
            "downloading": len(self._inflight),
            "done": len(self._done),
            "failed": len(self._failed),
        }

    def state(self, key: str) -> DownloadState | None:
        """Return the current state of ``key`` (or ``None`` if unknown)."""
        if key in self._done:
            return DownloadState.DONE
        if key in self._inflight:
            return DownloadState.DOWNLOADING
        if key in self._pending:
            return DownloadState.PENDING
        if key in self._failed:
            return DownloadState.FAILED
        return None

    # -- runner ------------------------------------------------------------

    async def run(self) -> None:
        """Run the scheduling loop until :meth:`stop` is called."""
        self._running = True
        self._sem = asyncio.Semaphore(self.concurrency)
        tasks: set[asyncio.Task[None]] = set()
        while self._running:
            now = self.clock()
            for key in self.due_keys(now):
                del self._pending[key]
                self._inflight.add(key)
                self._emit(key, DownloadState.DOWNLOADING)
                task = asyncio.create_task(self._download(self._items[key]))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), self._next_wait(now))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        """Ask the runner to finish (drains any in-flight downloads)."""
        self._running = False
        self._wake()

    def _next_wait(self, now: float) -> float | None:
        if not self._pending:
            return None
        return max(0.0, min(self._pending.values()) - now)

    async def _download(self, item: DownloadItem) -> None:
        assert self._sem is not None
        async with self._sem:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    await self.downloader(item)
                except Exception:  # any failure triggers a retry / mark-failed
                    if attempt >= self.max_attempts:
                        self._inflight.discard(item.key)
                        self._failed.add(item.key)
                        self._emit(item.key, DownloadState.FAILED)
                        return
                    continue
                else:
                    self._inflight.discard(item.key)
                    self._done.add(item.key)
                    self._emit(item.key, DownloadState.DONE)
                    return

    def _wake(self) -> None:
        self._changed.set()

    def _emit(self, key: str, state: DownloadState) -> None:
        if self.on_event is not None:
            self.on_event(key, state)
