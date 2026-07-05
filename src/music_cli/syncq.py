"""A debounced, bidirectional sync queue for the browse UI.

Each track has a *desired* end state — on the card (ticked) or off it
(un-ticked). Ticking or un-ticking sets that target and (re)starts a short
**debounce** timer; only once the timer settles does the queue act:

* target on-card, not there yet  → download it,
* target off-card, currently there or downloading → **abort any download and
  delete** the copy.

Because a flip resets the timer, a mistake corrected within the debounce window
does nothing at all — in either direction. The queue is generic: it is handed an
async ``download`` and an async ``remove`` callable, which keeps it unit-testable
with a fake clock and fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class TrackState(StrEnum):
    """Observable per-track state (for the progress footer)."""

    DOWNLOADING = "downloading"
    REMOVING = "removing"
    PRESENT = "present"
    ABSENT = "absent"
    FAILED = "failed"


class Intent(StrEnum):
    """The user's desired end state for a track."""

    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class DownloadItem:
    """A unit of work: a unique ``key`` plus an opaque ``payload``."""

    key: str
    payload: object = None


Downloader = Callable[[DownloadItem], Awaitable[None]]
Remover = Callable[[str], Awaitable[None]]
EventHook = Callable[[str, TrackState], None]


class SyncQueue:
    """Reconcile each track towards its debounced desired state."""

    def __init__(
        self,
        *,
        download: Downloader,
        remove: Remover,
        debounce: float = 5.0,
        concurrency: int = 3,
        max_attempts: int = 3,
        on_event: EventHook | None = None,
        clock: Callable[[], float] = time.monotonic,
        present: Iterable[str] = (),
    ) -> None:
        self._download = download
        self._remove = remove
        self.debounce = debounce
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self._on_event = on_event
        self._clock = clock

        self._items: dict[str, DownloadItem] = {}
        self._desired: dict[str, Intent] = {}
        self._deadline: dict[str, float] = {}
        self._state: dict[str, TrackState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._goal: dict[str, Intent] = {}
        self._done_count = 0

        self._running = False
        self._changed = asyncio.Event()
        self._sem: asyncio.Semaphore | None = None

        self.seed_present(present)

    # -- seeding & intent --------------------------------------------------

    def seed_present(self, keys: Iterable[str]) -> None:
        """Mark tracks already on the card as present (and thus ticked)."""
        for key in keys:
            self._state[key] = TrackState.PRESENT
            self._desired.setdefault(key, Intent.PRESENT)

    def select(self, item: DownloadItem) -> None:
        """Request that ``item`` end up on the card (debounced)."""
        self._items[item.key] = item
        self._request(item.key, Intent.PRESENT)

    def deselect(self, key: str) -> None:
        """Request that ``key`` end up off the card (debounced)."""
        self._request(key, Intent.ABSENT)

    def _request(self, key: str, intent: Intent) -> None:
        self._desired[key] = intent
        self._deadline[key] = self._clock() + self.debounce
        self._emit(key)
        self._wake()

    # -- queries -----------------------------------------------------------

    def wants_present(self, key: str) -> bool:
        """Whether the track's current *intent* is to be on the card (ticked)."""
        intent = self._desired.get(key)
        if intent is not None:
            return intent is Intent.PRESENT
        return self._state.get(key) is TrackState.PRESENT

    def state(self, key: str) -> TrackState | None:
        """Return the observable state of ``key`` (``None`` if unknown)."""
        return self._state.get(key)

    def failed_keys(self) -> list[str]:
        """Return the keys currently in the FAILED state."""
        return [k for k, s in self._state.items() if s is TrackState.FAILED]

    def retry_failed(self) -> int:
        """Re-queue every failed track whose item is known.

        Returns:
            The number of tracks re-queued.
        """
        count = 0
        for key in self.failed_keys():
            item = self._items.get(key)
            if item is not None:
                self.select(item)
                count += 1
        return count

    def title_of(self, key: str) -> str:
        """Best-effort display name for a key (payload title or the key)."""
        item = self._items.get(key)
        payload = getattr(item, "payload", None)
        return getattr(payload, "title", None) or key

    def counts(self) -> dict[str, int]:
        """Return totals for the progress footer."""
        counts = {
            "pending": len(self._deadline),
            "downloading": 0,
            "removing": 0,
            "failed": 0,
            "done": self._done_count,
        }
        for state in self._state.values():
            if state is TrackState.DOWNLOADING:
                counts["downloading"] += 1
            elif state is TrackState.REMOVING:
                counts["removing"] += 1
            elif state is TrackState.FAILED:
                counts["failed"] += 1
        return counts

    def due_keys(self, now: float) -> list[str]:
        """Return keys whose debounce window has elapsed by ``now``."""
        return [key for key, deadline in self._deadline.items() if deadline <= now]

    # -- runner ------------------------------------------------------------

    async def run(self) -> None:
        """Run the reconcile loop until :meth:`stop` is called."""
        self._running = True
        self._sem = asyncio.Semaphore(self.concurrency)
        while self._running:
            now = self._clock()
            for key in self.due_keys(now):
                del self._deadline[key]
                self._reconcile(key)
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), self._next_wait(now))

        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def stop(self) -> None:
        """Ask the runner to finish, cancelling any in-flight work."""
        self._running = False
        self._wake()

    def _reconcile(self, key: str) -> None:
        target = self._desired.get(key)
        if target is None:
            return
        running = self._tasks.get(key)
        if running is not None and not running.done():
            if self._goal.get(key) is target:
                return  # already working towards the desired state
            running.cancel()  # abort the opposite action (e.g. a download)
        current = self._state.get(key)
        if target is Intent.PRESENT:
            if current is not TrackState.PRESENT:
                self._start(key, Intent.PRESENT)
        # Only delete something that is actually on the card or mid-download;
        # un-ticking a track that was never there is a no-op.
        elif current in (TrackState.PRESENT, TrackState.DOWNLOADING):
            self._start(key, Intent.ABSENT)

    def _start(self, key: str, target: Intent) -> None:
        self._goal[key] = target
        if target is Intent.PRESENT:
            self._set(key, TrackState.DOWNLOADING)
            task = asyncio.create_task(self._run_download(key))
        else:
            self._set(key, TrackState.REMOVING)
            task = asyncio.create_task(self._run_remove(key))
        self._tasks[key] = task
        task.add_done_callback(functools.partial(self._task_done, key))

    def _task_done(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    async def _run_download(self, key: str) -> None:
        assert self._sem is not None
        item = self._items[key]
        async with self._sem:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    await self._download(item)
                except asyncio.CancelledError:
                    raise  # aborted by an un-tick; the remove task takes over
                except Exception:  # retry, or mark failed after last attempt
                    if attempt >= self.max_attempts:
                        self._set(key, TrackState.FAILED)
                        return
                    continue
                else:
                    self._done_count += 1
                    self._set(key, TrackState.PRESENT)
                    return

    async def _run_remove(self, key: str) -> None:
        with contextlib.suppress(Exception):
            await self._remove(key)
        self._set(key, TrackState.ABSENT)

    def _next_wait(self, now: float) -> float | None:
        if not self._deadline:
            return None
        return max(0.0, min(self._deadline.values()) - now)

    def _set(self, key: str, state: TrackState) -> None:
        self._state[key] = state
        self._emit(key)

    def _wake(self) -> None:
        self._changed.set()

    def _emit(self, key: str) -> None:
        if self._on_event is not None:
            self._on_event(key, self._state.get(key, TrackState.ABSENT))
