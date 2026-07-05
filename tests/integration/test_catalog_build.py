"""Integration tests for catalog build/update against a real temp library."""

from pathlib import Path

from music_cli import catalog
from music_cli.db import connect


def make_library(root: Path) -> None:
    """Create a small on-disk music library fixture."""
    files = [
        "Pink Floyd/The Wall/01 - In the Flesh.mp3",
        "Pink Floyd/The Wall/02 - The Thin Ice.mp3",
        "Queen/Greatest Hits/03 Bohemian Rhapsody.flac",
        "notes.txt",  # non-audio, ignored
    ]
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio-bytes" if path.suffix != ".txt" else b"nope")


async def _rows(db_path: Path) -> list[dict]:
    async with connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT rel_path, artist, album, title, track_no FROM tracks "
            "ORDER BY rel_path"
        )
        return [dict(r) for r in await cursor.fetchall()]


async def _meta(db_path: Path) -> dict[str, str]:
    async with connect(db_path) as conn:
        cursor = await conn.execute("SELECT key, value FROM meta")
        return {r["key"]: r["value"] for r in await cursor.fetchall()}


async def test_build_scans_audio_only(tmp_path: Path):
    make_library(tmp_path)
    db = tmp_path / "catalog.db"

    count = await catalog.build(tmp_path, db)

    assert count == 3  # notes.txt excluded
    rows = await _rows(db)
    assert [r["title"] for r in rows] == [
        "In the Flesh",
        "The Thin Ice",
        "Bohemian Rhapsody",
    ]
    first = rows[0]
    assert first["artist"] == "Pink Floyd"
    assert first["album"] == "The Wall"
    assert first["track_no"] == 1

    meta = await _meta(db)
    assert meta["schema_version"] == str(catalog.SCHEMA_VERSION)
    assert meta["track_count"] == "3"
    assert "generated_at" in meta


async def test_build_is_idempotent(tmp_path: Path):
    make_library(tmp_path)
    db = tmp_path / "catalog.db"
    await catalog.build(tmp_path, db)
    await catalog.build(tmp_path, db)
    rows = await _rows(db)
    assert len(rows) == 3  # no duplicates after rebuild


async def test_update_add_change_remove(tmp_path: Path):
    make_library(tmp_path)
    db = tmp_path / "catalog.db"
    await catalog.build(tmp_path, db)

    # Add a new track.
    new = tmp_path / "Queen/Greatest Hits/04 We Will Rock You.flac"
    new.write_bytes(b"audio-bytes")
    # Change an existing track's size.
    changed = tmp_path / "Pink Floyd/The Wall/01 - In the Flesh.mp3"
    changed.write_bytes(b"much-longer-audio-bytes")
    # Remove one.
    (tmp_path / "Pink Floyd/The Wall/02 - The Thin Ice.mp3").unlink()

    result = await catalog.update(tmp_path, db)

    assert result.added == 1
    assert result.changed == 1
    assert result.removed == 1
    rels = [r["rel_path"] for r in await _rows(db)]
    assert "Queen/Greatest Hits/04 We Will Rock You.flac" in rels
    assert "Pink Floyd/The Wall/02 - The Thin Ice.mp3" not in rels
