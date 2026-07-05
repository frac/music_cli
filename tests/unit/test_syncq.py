"""Tests for the debounced bidirectional sync queue."""

import asyncio

import pytest

from music_cli.syncq import DownloadItem
from music_cli.syncq import SyncQueue
from music_cli.syncq import TrackState


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _item(key: str) -> DownloadItem:
    return DownloadItem(key=key, payload=key)


async def _noop_download(_: DownloadItem) -> None:
    return None


async def _noop_remove(_: str) -> None:
    return None


def _queue(**kw) -> SyncQueue:
    kw.setdefault("download", _noop_download)
    kw.setdefault("remove", _noop_remove)
    return SyncQueue(**kw)


# -- pure intent / debounce logic (no event loop) --------------------------


def test_intent_and_due():
    clock = FakeClock(0.0)
    q = _queue(debounce=5.0, clock=clock)

    assert q.wants_present("a") is False
    q.select(_item("a"))
    assert q.wants_present("a") is True
    assert q.due_keys(4.9) == []
    assert q.due_keys(5.0) == ["a"]

    q.deselect("a")
    assert q.wants_present("a") is False


def test_seed_present_marks_ticked():
    q = _queue(present=["a"])
    assert q.wants_present("a") is True
    assert q.state("a") is TrackState.PRESENT


# -- runner behaviour -------------------------------------------------------


async def _run(q: SyncQueue):
    task = asyncio.create_task(q.run())
    return task


async def test_select_downloads_after_debounce():
    downloaded: list[str] = []
    done = asyncio.Event()

    async def dl(item: DownloadItem) -> None:
        downloaded.append(item.key)

    def ev(_key: str, state: TrackState) -> None:
        if state is TrackState.PRESENT:
            done.set()

    q = _queue(download=dl, debounce=0.02, on_event=ev)
    task = await _run(q)

    q.select(_item("song"))
    await asyncio.wait_for(done.wait(), 1.0)
    assert downloaded == ["song"]
    assert q.state("song") is TrackState.PRESENT

    q.stop()
    await task


async def test_select_then_deselect_within_grace_does_nothing():
    downloaded: list[str] = []
    removed: list[str] = []

    async def dl(item: DownloadItem) -> None:
        downloaded.append(item.key)

    async def rm(key: str) -> None:
        removed.append(key)

    q = _queue(download=dl, remove=rm, debounce=0.05)
    task = await _run(q)

    q.select(_item("oops"))
    q.deselect("oops")  # corrected well within the window
    await asyncio.sleep(0.15)
    assert downloaded == []
    assert removed == []

    q.stop()
    await task


async def test_deselect_present_removes_after_debounce():
    removed: list[str] = []
    gone = asyncio.Event()

    async def rm(key: str) -> None:
        removed.append(key)

    def ev(_key: str, state: TrackState) -> None:
        if state is TrackState.ABSENT:
            gone.set()

    q = _queue(remove=rm, debounce=0.02, on_event=ev, present=["a"])
    task = await _run(q)

    q.deselect("a")
    await asyncio.wait_for(gone.wait(), 1.0)
    assert removed == ["a"]
    assert q.state("a") is TrackState.ABSENT

    q.stop()
    await task


async def test_deselect_then_reselect_within_grace_keeps_file():
    removed: list[str] = []

    async def rm(key: str) -> None:
        removed.append(key)

    q = _queue(remove=rm, debounce=0.05, present=["a"])
    task = await _run(q)

    q.deselect("a")
    q.select(_item("a"))  # changed my mind within the window
    await asyncio.sleep(0.15)
    assert removed == []  # never deleted
    assert q.wants_present("a") is True

    q.stop()
    await task


async def test_untick_aborts_in_flight_download_then_removes():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    gone = asyncio.Event()
    removed: list[str] = []

    async def dl(_: DownloadItem) -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # block forever until cancelled
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def rm(key: str) -> None:
        removed.append(key)

    def ev(_key: str, state: TrackState) -> None:
        if state is TrackState.ABSENT:
            gone.set()

    q = _queue(download=dl, remove=rm, debounce=0.02, on_event=ev)
    task = await _run(q)

    q.select(_item("a"))
    await asyncio.wait_for(started.wait(), 1.0)  # download is in flight
    q.deselect("a")
    await asyncio.wait_for(cancelled.wait(), 1.0)  # download aborted
    await asyncio.wait_for(gone.wait(), 1.0)

    assert cancelled.is_set()  # the download was aborted
    assert removed == ["a"]  # then the copy was deleted
    assert q.state("a") is TrackState.ABSENT

    q.stop()
    await task


async def test_download_retries_then_fails():
    attempts = 0
    failed = asyncio.Event()

    async def dl(_: DownloadItem) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("boom")

    def ev(_key: str, state: TrackState) -> None:
        if state is TrackState.FAILED:
            failed.set()

    q = _queue(download=dl, debounce=0.01, max_attempts=3, on_event=ev)
    task = await _run(q)

    q.select(_item("bad"))
    await asyncio.wait_for(failed.wait(), 1.0)
    assert attempts == 3
    assert q.state("bad") is TrackState.FAILED

    q.stop()
    await task


@pytest.mark.parametrize("n", [1, 2])
async def test_download_recovers_on_retry(n: int):
    calls = 0
    done = asyncio.Event()

    async def dl(_: DownloadItem) -> None:
        nonlocal calls
        calls += 1
        if calls <= n:
            raise OSError("transient")

    def ev(_key: str, state: TrackState) -> None:
        if state is TrackState.PRESENT:
            done.set()

    q = _queue(download=dl, debounce=0.01, max_attempts=5, on_event=ev)
    task = await _run(q)

    q.select(_item("ok"))
    await asyncio.wait_for(done.wait(), 1.0)
    assert calls == n + 1

    q.stop()
    await task
