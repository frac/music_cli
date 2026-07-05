"""Tests for settings persistence and server/dest resolution."""

from pathlib import Path

import pytest

from music_cli import config
from music_cli.config import ResolutionError
from music_cli.config import Settings
from music_cli.device import DEVICE_DB_NAME


def test_save_load_roundtrip(tmp_path: Path):
    path = tmp_path / "config.toml"
    config.save(Settings(server="http://box:8000", dest="/media/kid/CARD"), path)
    loaded = config.load(path)
    assert loaded.server == "http://box:8000"
    assert loaded.dest == "/media/kid/CARD"


def test_load_missing_or_garbage_is_empty(tmp_path: Path):
    assert config.load(tmp_path / "nope.toml") == Settings()
    bad = tmp_path / "bad.toml"
    bad.write_text("not [valid")
    assert config.load(bad) == Settings()


def test_save_escapes_quotes_and_backslashes(tmp_path: Path):
    path = tmp_path / "config.toml"
    tricky = 'C:\\Users\\kid\\"music"'
    config.save(Settings(server="s", dest=tricky), path)
    assert config.load(path).dest == tricky


def _card(tmp_path: Path, name: str) -> Path:
    card = tmp_path / name
    card.mkdir(parents=True)
    (card / DEVICE_DB_NAME).write_bytes(b"")
    return card


def test_detect_cards_scans_two_levels(tmp_path: Path):
    direct = _card(tmp_path / "media", "CARD1")  # /media/CARD1
    nested = _card(tmp_path / "media" / "kid", "CARD2")  # /media/kid/CARD2
    (tmp_path / "media" / "empty").mkdir()  # no device db → ignored

    found = config.detect_cards(roots=(str(tmp_path / "media"),))
    assert set(found) == {direct, nested}


def test_resolve_flag_beats_everything(tmp_path: Path):
    server, dest = config.resolve(
        "http://flag:1",
        tmp_path,
        settings=Settings(server="http://saved:2", dest="/saved"),
        cards=[Path("/media/x")],
    )
    assert server == "http://flag:1"
    assert dest == tmp_path


def test_resolve_unique_card_beats_saved_dest(tmp_path: Path):
    card = tmp_path / "CARD"
    card.mkdir()
    server, dest = config.resolve(
        None,
        None,
        settings=Settings(server="http://saved:2", dest=str(tmp_path)),
        cards=[card],
    )
    assert server == "http://saved:2"
    assert dest == card


def test_resolve_falls_back_to_saved_dir(tmp_path: Path):
    _server, dest = config.resolve(
        None,
        None,
        settings=Settings(server="http://saved:2", dest=str(tmp_path)),
        cards=[],
    )
    assert dest == tmp_path


def test_resolve_errors():
    with pytest.raises(ResolutionError, match="No server"):
        config.resolve(None, None, settings=Settings(), cards=[])
    with pytest.raises(ResolutionError, match="Multiple cards"):
        config.resolve(
            "http://s",
            None,
            settings=Settings(),
            cards=[Path("/a"), Path("/b")],
        )
    with pytest.raises(ResolutionError, match="No destination"):
        config.resolve("http://s", None, settings=Settings(), cards=[])
    # A remembered dest that is not currently a directory must not be used.
    with pytest.raises(ResolutionError, match="does not exist"):
        config.resolve(
            "http://s",
            None,
            settings=Settings(dest="/definitely/not/mounted"),
            cards=[],
        )
