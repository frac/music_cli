"""Tests for safe deletion of card tracks (device.remove_copied)."""

from pathlib import Path

from music_cli.device import load_copied
from music_cli.device import record_copied
from music_cli.device import remove_copied

REL = "Queen/Greatest Hits/03 Bohemian Rhapsody.flac"


async def _place(card: Path, rel: str, data: bytes = b"audio") -> Path:
    """Copy a file onto the card and record it, as a real sync would."""
    target = card / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    await record_copied(card, rel, len(data), 1, "2026-07-05T00:00:00")
    return target


async def test_removes_recorded_file_and_prunes_dirs(tmp_path: Path):
    card = tmp_path / "card"
    target = await _place(card, REL)

    deleted = await remove_copied(card, REL)

    assert deleted is True
    assert not target.exists()
    # Empty album/artist dirs are pruned, but the card root survives.
    assert not (card / "Queen").exists()
    assert card.exists()
    assert await load_copied(card) == {}  # record cleared


async def test_refuses_file_we_did_not_record(tmp_path: Path):
    card = tmp_path / "card"
    await _place(card, REL)  # ensures a device DB exists

    # A file the DB has no record of — must never be deleted.
    intruder = card / "Someone Else/track.mp3"
    intruder.parent.mkdir(parents=True)
    intruder.write_bytes(b"not ours")

    deleted = await remove_copied(card, "Someone Else/track.mp3")

    assert deleted is False
    assert intruder.read_bytes() == b"not ours"


async def test_refuses_symlink_escape(tmp_path: Path):
    card = tmp_path / "card"
    outside = tmp_path / "other_music" / "precious.flac"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"do not touch")

    # A symlink on the card pointing at music in another directory, recorded
    # in the DB as if it were ours.
    link_rel = "Various/link.flac"
    link = card / link_rel
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    await record_copied(card, link_rel, 0, 1, "x")

    deleted = await remove_copied(card, link_rel)

    assert deleted is False
    assert outside.read_bytes() == b"do not touch"  # target untouched
    # The stale record is still cleared.
    assert link_rel not in await load_copied(card)


async def test_missing_device_db_is_noop(tmp_path: Path):
    assert await remove_copied(tmp_path / "empty", REL) is False
