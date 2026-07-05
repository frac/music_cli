"""Integration tests for the client + device DB + sync orchestration."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from music_cli import catalog
from music_cli.client import MusicClient
from music_cli.server import create_app
from music_cli.sync import status_summary
from music_cli.sync import sync_tracks
from tests.integration.test_catalog_build import make_library


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[MusicClient, Path]]:
    """A MusicClient wired to an in-process server, plus a fresh card dir."""
    library = tmp_path / "library"
    library.mkdir()
    make_library(library)
    db = tmp_path / "catalog.db"
    await catalog.build(library, db)

    app = create_app(library, db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        card = tmp_path / "card"
        yield MusicClient("http://test", http), card


async def test_sync_copies_all_then_is_idempotent(client):
    music, card = client

    copied = await sync_tracks(music, card)
    assert copied == 3

    track = card / "Pink Floyd/The Wall/01 - In the Flesh.mp3"
    assert track.read_bytes() == b"audio-bytes"

    # Second run copies nothing.
    assert await sync_tracks(music, card) == 0

    summary = await status_summary(music, card)
    assert summary == summary.__class__(on_card=3, available=3, to_copy=0)


async def test_sync_filters_by_artist(client):
    music, card = client

    copied = await sync_tracks(music, card, artist="Queen")
    assert copied == 1
    assert (card / "Queen/Greatest Hits/03 Bohemian Rhapsody.flac").exists()
    assert not (card / "Pink Floyd").exists()


async def test_changed_file_is_recopied(client, tmp_path: Path):
    music, card = client
    await sync_tracks(music, card)

    # Change the library file and rebuild the catalog.
    library = tmp_path / "library"
    (library / "Queen/Greatest Hits/03 Bohemian Rhapsody.flac").write_bytes(
        b"a-much-longer-remastered-version"
    )
    await catalog.build(library, tmp_path / "catalog.db")

    # Force a catalog refresh by clearing the cached etag sidecar.
    (card / ".music_cli.catalog.db.etag").unlink()

    recopied = await sync_tracks(music, card)
    assert recopied == 1
    assert (
        card / "Queen/Greatest Hits/03 Bohemian Rhapsody.flac"
    ).read_bytes() == b"a-much-longer-remastered-version"
