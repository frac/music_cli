"""Unit tests for path/filename metadata parsing (no I/O)."""

import pytest

from music_cli.catalog import parse_meta
from music_cli.catalog import parse_title


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("01 - In the Flesh", (1, "In the Flesh")),
        ("01. In the Flesh", (1, "In the Flesh")),
        ("01 In the Flesh", (1, "In the Flesh")),
        ("1-Song", (1, "Song")),
        ("12 – Dashed", (12, "Dashed")),  # noqa: RUF001 (en-dash separator)
        ("Untitled", (None, "Untitled")),
        ("1999", (None, "1999")),  # all digits, no title → not a track number
        ("  03   Spaces  ", (3, "Spaces")),
    ],
)
def test_parse_title(stem, expected):
    assert parse_title(stem) == expected


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        (
            "Pink Floyd/The Wall/01 - In the Flesh.mp3",
            ("Pink Floyd", "The Wall", 1, "In the Flesh"),
        ),
        (
            "Queen/Greatest Hits/03 Bohemian Rhapsody.flac",
            ("Queen", "Greatest Hits", 3, "Bohemian Rhapsody"),
        ),
        # Single directly under the artist folder: artist, no album.
        (
            "Duran Duran/Duran Duran - Girls on Film.mp3",
            ("Duran Duran", None, None, "Duran Duran - Girls on Film"),
        ),
        ("Queen/We Will Rock You.mp3", ("Queen", None, None, "We Will Rock You")),
        # Bare file at the root: no artist, no album.
        ("loose.mp3", (None, None, None, "loose")),
        # Backslashes normalise to forward slashes.
        (
            "A\\B\\04 - C.ogg",
            ("A", "B", 4, "C"),
        ),
    ],
)
def test_parse_meta(rel_path, expected):
    assert parse_meta(rel_path) == expected
