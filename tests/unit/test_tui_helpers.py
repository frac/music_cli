"""Unit tests for the pure TUI helper functions."""

from music_cli.catalog import CatalogTrack
from music_cli.syncq import DownloadItem
from music_cli.tui import format_status
from music_cli.tui import track_item


def _track(rel: str) -> CatalogTrack:
    return CatalogTrack(
        rel_path=rel,
        artist="A",
        album="B",
        title="T",
        track_no=1,
        size_bytes=10,
        mtime_ns=1,
    )


def test_track_item_keys_by_rel_path():
    track = _track("A/B/01 - T.mp3")
    item = track_item(track)
    assert isinstance(item, DownloadItem)
    assert item.key == "A/B/01 - T.mp3"
    assert item.payload is track


def test_format_status_renders_all_counts():
    line = format_status({"pending": 2, "downloading": 1, "done": 5, "failed": 3})
    assert "2 pending" in line
    assert "1 copying" in line
    assert "5 done" in line
    assert "3 failed" in line


def test_format_status_defaults_missing_to_zero():
    assert "0 pending" in format_status({})
