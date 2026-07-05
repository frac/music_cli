"""Textual Pilot tests for the browse app (headless key-driven)."""

from pathlib import Path

from music_cli import catalog
from music_cli.syncq import DownloadItem
from music_cli.syncq import DownloadState
from music_cli.tui import BrowseApp


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
    app = BrowseApp(cache=cache, downloader=_noop, debounce=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Root is expanded; move to the first artist and tick it.
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()

    # Both the album track and the single got queued.
    duran = await catalog.query_tracks(cache, artist="Duran Duran")
    assert app._queue.counts()["pending"] == len(duran) == 2
    for track in duran:
        assert app._queue.state(track.rel_path) is DownloadState.PENDING


async def test_space_again_deselects(tmp_path: Path):
    cache = await _cache(tmp_path)

    async def _noop(_: DownloadItem) -> None:
        return None

    app = BrowseApp(cache=cache, downloader=_noop, debounce=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("space")  # select artist
        await pilot.pause()
        assert app._queue.counts()["pending"] == 2
        await pilot.press("space")  # deselect artist
        await pilot.pause()

    assert app._queue.counts()["pending"] == 0
