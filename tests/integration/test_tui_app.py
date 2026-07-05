"""Textual Pilot tests for the browse app (headless key-driven)."""

import asyncio
from pathlib import Path

from music_cli import catalog
from music_cli.device import load_copied
from music_cli.device import record_copied
from music_cli.syncq import DownloadItem
from music_cli.tui import BrowseApp


async def _noop(_: DownloadItem) -> None:
    return None


async def _preload_card(card: Path, tracks) -> None:
    """Place tracks on the card and record them, as a prior sync would."""
    for track in tracks:
        path = card / track.rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        await record_copied(card, track.rel_path, 5, track.mtime_ns, "x")


def _make_library(root: Path) -> None:
    files = [
        # An album track and a single, both under the same artist.
        "Duran Duran/Decade/06 - Is There Something I Should Know.mp3",
        "Duran Duran/Duran Duran - Girls on Film.mp3",
        # A loose file at the root (no artist).
        "orphan.mp3",
    ]
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")


async def _cache(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    _make_library(lib)
    db = tmp_path / "catalog.db"
    await catalog.build(lib, db)
    return db


async def test_singles_and_loose_are_reachable(tmp_path: Path):
    cache = await _cache(tmp_path)
    # A single now carries its artist, and the orphan is a loose track.
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    assert any(t.album is None for t in duran)  # the single
    assert any(t.album == "Decade" for t in duran)  # the album track
    assert len(await catalog.list_loose(cache)) == 1


async def test_space_selects_whole_artist(tmp_path: Path):
    cache = await _cache(tmp_path)

    async def _noop(_: DownloadItem) -> None:  # never fires (long debounce)
        return None

    # Long debounce so selected tracks stay PENDING for assertions.
    app = BrowseApp(
        cache=cache, downloader=_noop, dest=tmp_path / "card", debounce=60.0
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Root is expanded; move to the first artist and tick it.
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()

    # Both the album track and the single got ticked (queued for download).
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    assert app._queue.counts()["pending"] == len(duran) == 2
    for track in duran:
        assert app._queue.wants_present(track.rel_path) is True


async def test_space_again_deselects(tmp_path: Path):
    cache = await _cache(tmp_path)

    async def _noop(_: DownloadItem) -> None:
        return None

    app = BrowseApp(
        cache=cache, downloader=_noop, dest=tmp_path / "card", debounce=60.0
    )
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("space")  # tick artist
        await pilot.pause()
        assert all(app._queue.wants_present(t.rel_path) for t in duran)
        await pilot.press("space")  # un-tick artist
        await pilot.pause()

    assert not any(app._queue.wants_present(t.rel_path) for t in duran)


async def test_present_tracks_start_checked(tmp_path: Path):
    cache = await _cache(tmp_path)
    card = tmp_path / "card"
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    await _preload_card(card, duran)

    app = BrowseApp(cache=cache, downloader=_noop, dest=card, debounce=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._tree is not None
        artist_node = app._tree.root.children[0]
        # Fully-present artist is pre-ticked on open.
        assert artist_node.data is not None
        assert artist_node.data["checked"] is True
        assert "☑" in str(artist_node.label)


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate(), "condition not met within timeout"


async def test_uncheck_deletes_from_card(tmp_path: Path):
    cache = await _cache(tmp_path)
    card = tmp_path / "card"
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    await _preload_card(card, duran)

    # Short debounce so the deletion actually fires during the test.
    app = BrowseApp(cache=cache, downloader=_noop, dest=card, debounce=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # onto the (pre-ticked) artist
        await pilot.press("space")  # un-tick → delete after the grace window
        await _wait_until(lambda: not any((card / t.rel_path).exists() for t in duran))
        assert await load_copied(card) == {}


async def test_untick_then_retick_within_grace_keeps_files(tmp_path: Path):
    cache = await _cache(tmp_path)
    card = tmp_path / "card"
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    await _preload_card(card, duran)

    app = BrowseApp(cache=cache, downloader=_noop, dest=card, debounce=0.3)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("space")  # un-tick
        await pilot.press("space")  # ...changed my mind, well within 0.3s
        await asyncio.sleep(0.5)  # let the (cancelled) window pass

    # Nothing was deleted: the mistake was corrected in time.
    for track in duran:
        assert (card / track.rel_path).exists()
    assert set(await load_copied(card)) == {t.rel_path for t in duran}
