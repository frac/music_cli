"""Unit tests for the debounced background download queue."""

import asyncio

import pytest

from music_cli.syncq import DownloadItem
from music_cli.syncq import DownloadQueue
from music_cli.syncq import DownloadState


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _item(key: str) -> DownloadItem:
    return DownloadItem(key=key, payload=key)


async def _noop(_: DownloadItem) -> None:
    return None


# -- pure debounce logic (no event loop needed) ----------------------------


def test_select_becomes_due_only_after_debounce():
    clock = FakeClock(0.0)
    q = DownloadQueue(downloader=_noop, debounce=5.0, clock=clock)

    q.select(_item("a"))
    assert q.state("a") is DownloadState.PENDING
    clock.t = 4.9
    assert q.due_keys(clock.t) == []
    clock.t = 5.0
    assert q.due_keys(clock.t) == ["a"]


def test_deselect_before_debounce_cancels():
    clock = FakeClock(0.0)
    events: list[tuple[str, DownloadState]] = []
    q = DownloadQueue(
        downloader=_noop,
        debounce=5.0,
        clock=clock,
        on_event=lambda k, s: events.append((k, s)),
    )

    q.select(_item("a"))
    clock.t = 3.0
    q.deselect("a")
    assert ("a", DownloadState.CANCELLED) in events
    # Cancelled items become schedulable-from-scratch again (transient state).
    assert q.state("a") is None
    clock.t = 10.0
    assert q.due_keys(clock.t) == []


def test_reselect_is_deduplicated():
    clock = FakeClock(0.0)
    q = DownloadQueue(downloader=_noop, debounce=5.0, clock=clock)

    q.select(_item("a"))
    clock.t = 2.0
    q.select(_item("a"))  # ignored; deadline stays at 5.0
    clock.t = 5.0
    assert q.due_keys(clock.t) == ["a"]


# -- async runner behaviour -------------------------------------------------


async def test_runner_downloads_after_debounce():
    downloaded: list[str] = []
    done = asyncio.Event()

    async def downloader(item: DownloadItem) -> None:
        downloaded.append(item.key)

    def on_event(key: str, state: DownloadState) -> None:
        if state is DownloadState.DONE:
            done.set()

    q = DownloadQueue(downloader=downloader, debounce=0.02, on_event=on_event)
    runner = asyncio.create_task(q.run())

    q.select(_item("song"))
    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert downloaded == ["song"]
    assert q.state("song") is DownloadState.DONE

    q.stop()
    await runner


async def test_runner_skips_cancelled_selection():
    downloaded: list[str] = []

    async def downloader(item: DownloadItem) -> None:
        downloaded.append(item.key)

    q = DownloadQueue(downloader=downloader, debounce=0.05)
    runner = asyncio.create_task(q.run())

    q.select(_item("oops"))
    q.deselect("oops")  # cancelled well before the 50ms window
    await asyncio.sleep(0.15)
    assert downloaded == []

    q.stop()
    await runner


async def test_runner_retries_then_marks_failed():
    attempts = 0
    failed = asyncio.Event()

    async def flaky(_: DownloadItem) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("boom")

    def on_event(key: str, state: DownloadState) -> None:
        if state is DownloadState.FAILED:
            failed.set()

    q = DownloadQueue(
        downloader=flaky, debounce=0.01, max_attempts=3, on_event=on_event
    )
    runner = asyncio.create_task(q.run())

    q.select(_item("bad"))
    await asyncio.wait_for(failed.wait(), timeout=1.0)
    assert attempts == 3
    assert q.state("bad") is DownloadState.FAILED

    q.stop()
    await runner


@pytest.mark.parametrize("n", [1, 2])
async def test_runner_recovers_on_retry(n: int):
    calls = 0
    done = asyncio.Event()

    async def flaky(_: DownloadItem) -> None:
        nonlocal calls
        calls += 1
        if calls <= n:
            raise OSError("transient")

    def on_event(key: str, state: DownloadState) -> None:
        if state is DownloadState.DONE:
            done.set()

    q = DownloadQueue(
        downloader=flaky, debounce=0.01, max_attempts=5, on_event=on_event
    )
    runner = asyncio.create_task(q.run())

    q.select(_item("ok"))
    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert calls == n + 1

    q.stop()
    await runner
