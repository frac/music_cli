"""Tests for FAT32/exFAT-safe destination path sanitization."""

import pytest

from music_cli.device import card_path
from music_cli.device import safe_rel_path


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        # Legal names pass through untouched.
        (
            "Queen/Greatest Hits/03 Bohemian Rhapsody.flac",
            "Queen/Greatest Hits/03 Bohemian Rhapsody.flac",
        ),
        # FAT-illegal characters are replaced per segment.
        ('AC_DC/Live: "Wired"/01 - T.N.T?.mp3', "AC_DC/Live_ _Wired_/01 - T.N.T_.mp3"),
        ("a<b>c/track|no*.mp3", "a_b_c/track_no_.mp3"),
        # Trailing dots/spaces are illegal on FAT — stripped.
        ("Artist./Album /song.mp3.", "Artist/Album/song.mp3"),
        # Windows reserved device names get prefixed.
        ("CON/aux.mp3", "_CON/_aux.mp3"),
        # Control characters are replaced.
        ("A\x01B/ok.mp3", "A_B/ok.mp3"),
    ],
)
def test_safe_rel_path(rel, expected):
    assert safe_rel_path(rel) == expected


def test_card_path_joins_sanitized(tmp_path):
    target = card_path(tmp_path, "X: Y/z?.mp3")
    assert target == tmp_path / "X_ Y" / "z_.mp3"


def test_segment_never_empties():
    assert safe_rel_path("../..../x.mp3") != ""
    assert "" not in safe_rel_path("  . /x.mp3").split("/")
