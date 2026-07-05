"""Integration test for the queue downloader built by the TUI layer."""

from pathlib import Path

from music_cli.catalog import CatalogTrack
from music_cli.device import load_copied
from music_cli.syncq import DownloadItem
from music_cli.tui import make_downloader


class StubClient:
    """A MusicClient stand-in that writes fixed bytes for any track."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def download_track(self, rel_path: str, dest: Path) -> int:
        Path(dest).write_bytes(b"payload")
        self.requested.append(rel_path)
        return 7


async def test_make_downloader_copies_and_records(tmp_path: Path):
    card = tmp_path / "card"
    client = StubClient()
    downloader = make_downloader(client, card, generated_at="2026-07-05T00:00:00")

    track = CatalogTrack(
        rel_path="Queen/Hits/03 Bohemian Rhapsody.flac",
        artist="Queen",
        album="Hits",
        title="Bohemian Rhapsody",
        track_no=3,
        size_bytes=7,
        mtime_ns=42,
    )
    await downloader(DownloadItem(key=track.rel_path, payload=track))

    written = card / "Queen/Hits/03 Bohemian Rhapsody.flac"
    assert written.read_bytes() == b"payload"
    assert client.requested == [track.rel_path]

    copied = await load_copied(card)
    assert copied == {track.rel_path: 7}
