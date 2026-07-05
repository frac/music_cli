"""Unit tests for the pure TUI helper functions."""

from music_cli.catalog import CatalogTrack
from music_cli.syncq import DownloadItem
from music_cli.tui import checkbox_markup
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
    line = format_status(
        {"pending": 2, "downloading": 1, "removing": 4, "done": 5, "failed": 3}
    )
    assert "2 pending" in line
    assert "1 copying" in line
    assert "4 removing" in line
    assert "5 done" in line
    assert "3 failed" in line


def test_format_status_defaults_missing_to_zero():
    assert "0 pending" in format_status({})


def test_checkbox_markup_toggles():
    assert checkbox_markup("Queen", checked=False) == "☐ Queen"
    assert checkbox_markup("Queen", checked=True) == "[green]☑[/] Queen"
    assert checkbox_markup("Queen", checked=None) == "[yellow]◐[/] Queen"


def test_human_size():
    from music_cli.tui import human_size

    assert human_size(0) == "0 B"
    assert human_size(999) == "999 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert human_size(3 * 1024**3) == "3.0 GB"
    assert human_size(5000 * 1024**3) == "5000.0 GB"
