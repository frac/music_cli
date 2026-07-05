"""Build a SQLite catalog of a music library from its folder structure.

Metadata (artist / album / title / track number) is derived purely from the
on-disk layout ``Artist/Album/NN - Title.ext`` — no audio tags are read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path

import aiosqlite

from music_cli.db import connect

SCHEMA_VERSION = 1

#: Recognised audio file extensions (lower-case, without the leading dot).
AUDIO_EXTENSIONS = frozenset({"mp3", "flac", "ogg", "m4a", "aac", "wav", "wma", "opus"})

#: ``01 - Title`` / ``01. Title`` / ``1 Title`` … → (track_no, title).
_TRACK_RE = re.compile(r"^(\d{1,3})[\s._\-–)]+(.+)$")  # noqa: RUF001 (en dash sep)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY,
    rel_path   TEXT    NOT NULL UNIQUE,
    artist     TEXT,
    album      TEXT,
    title      TEXT,
    track_no   INTEGER,
    ext        TEXT,
    size_bytes INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_album ON tracks(artist, album);
"""


@dataclass(frozen=True, slots=True)
class TrackMeta:
    """A single catalog row derived from a file path plus its stat data."""

    rel_path: str
    artist: str | None
    album: str | None
    title: str
    track_no: int | None
    ext: str
    size_bytes: int
    mtime_ns: int


def parse_title(stem: str) -> tuple[int | None, str]:
    """Split a filename stem into an optional track number and a title.

    Args:
        stem: Filename without directory or extension, e.g. ``"01 - In the Flesh"``.

    Returns:
        ``(track_no, title)``. ``track_no`` is ``None`` when no leading number
        followed by a separator and a non-empty remainder is present.
    """
    match = _TRACK_RE.match(stem.strip())
    if match:
        return int(match.group(1)), match.group(2).strip()
    return None, stem.strip()


def parse_meta(rel_path: str) -> tuple[str | None, str | None, int | None, str]:
    """Derive ``(artist, album, track_no, title)`` from a relative path.

    The two directories immediately above the file are treated as
    ``artist`` and ``album`` respectively; shallower layouts degrade
    gracefully to ``None``.

    Args:
        rel_path: POSIX-style path relative to the library root.

    Returns:
        ``(artist, album, track_no, title)``.
    """
    parts = _split_posix(rel_path)
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    track_no, title = parse_title(stem)
    album = parts[-2] if len(parts) >= 2 else None
    artist = parts[-3] if len(parts) >= 3 else None
    return artist, album, track_no, title


def _split_posix(rel_path: str) -> list[str]:
    """Return non-empty path segments of a POSIX-style relative path."""
    return [p for p in rel_path.replace("\\", "/").split("/") if p]


def iter_tracks(root: str | Path) -> Iterator[TrackMeta]:
    """Yield :class:`TrackMeta` for every audio file under ``root``.

    Args:
        root: Library root directory.

    Yields:
        One :class:`TrackMeta` per recognised audio file, in sorted path order.
    """
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower().lstrip(".")
        if ext not in AUDIO_EXTENSIONS:
            continue
        rel_path = path.relative_to(root).as_posix()
        artist, album, track_no, title = parse_meta(rel_path)
        stat = path.stat()
        yield TrackMeta(
            rel_path=rel_path,
            artist=artist,
            album=album,
            title=title,
            track_no=track_no,
            ext=ext,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )


_INSERT = """
INSERT INTO tracks
    (rel_path, artist, album, title, track_no, ext, size_bytes, mtime_ns)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(rel_path) DO UPDATE SET
    artist=excluded.artist, album=excluded.album, title=excluded.title,
    track_no=excluded.track_no, ext=excluded.ext,
    size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns
"""


def _row(m: TrackMeta) -> tuple[object, ...]:
    return (
        m.rel_path,
        m.artist,
        m.album,
        m.title,
        m.track_no,
        m.ext,
        m.size_bytes,
        m.mtime_ns,
    )


async def _write_meta(conn: aiosqlite.Connection, track_count: int) -> str:
    generated_at = datetime.now(UTC).isoformat()
    values = {
        "schema_version": str(SCHEMA_VERSION),
        "generated_at": generated_at,
        "track_count": str(track_count),
    }
    await conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(values.items()),
    )
    return generated_at


@dataclass(frozen=True, slots=True)
class CatalogTrack:
    """A track row read back out of the catalog (client- or server-side)."""

    rel_path: str
    artist: str | None
    album: str | None
    title: str
    track_no: int | None
    size_bytes: int
    mtime_ns: int


async def read_meta(db_path: str | Path) -> dict[str, str]:
    """Return the catalog's ``meta`` key/value pairs."""
    async with connect(db_path) as conn:
        cursor = await conn.execute("SELECT key, value FROM meta")
        return {row["key"]: row["value"] for row in await cursor.fetchall()}


_QUERY_COLUMNS = "rel_path, artist, album, title, track_no, size_bytes, mtime_ns"


async def query_tracks(
    db_path: str | Path,
    *,
    artist: str | None = None,
    album: str | None = None,
) -> list[CatalogTrack]:
    """Return catalog tracks, optionally filtered by artist and/or album.

    Args:
        db_path: Path to a catalog database.
        artist: Restrict to this exact artist when given.
        album: Restrict to this exact album when given.

    Returns:
        Tracks ordered by artist, album, track number then path.
    """
    clauses: list[str] = []
    params: list[object] = []
    if artist is not None:
        clauses.append("artist = ?")
        params.append(artist)
    if album is not None:
        clauses.append("album = ?")
        params.append(album)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {_QUERY_COLUMNS} FROM tracks{where} "
        "ORDER BY artist, album, track_no, rel_path"
    )
    async with connect(db_path) as conn:
        cursor = await conn.execute(sql, params)
        return [CatalogTrack(**dict(row)) for row in await cursor.fetchall()]


async def list_artists(db_path: str | Path) -> list[str]:
    """Return the distinct, sorted list of artists in the catalog."""
    async with connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT artist FROM tracks "
            "WHERE artist IS NOT NULL ORDER BY artist"
        )
        return [row["artist"] for row in await cursor.fetchall()]


async def list_albums(db_path: str | Path, artist: str) -> list[str]:
    """Return the distinct, sorted albums for ``artist``."""
    async with connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT album FROM tracks "
            "WHERE artist = ? AND album IS NOT NULL ORDER BY album",
            (artist,),
        )
        return [row["album"] for row in await cursor.fetchall()]


async def build(root: str | Path, db_path: str | Path) -> int:
    """Build the catalog from scratch, replacing any existing rows.

    Args:
        root: Library root directory to scan.
        db_path: Destination SQLite catalog path.

    Returns:
        The number of tracks written.
    """
    tracks = list(iter_tracks(root))
    async with connect(db_path) as conn:
        await conn.executescript(SCHEMA)
        await conn.execute("DELETE FROM tracks")
        await conn.executemany(_INSERT, [_row(m) for m in tracks])
        await _write_meta(conn, len(tracks))
        await conn.commit()
    return len(tracks)


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Summary of an incremental :func:`update` run."""

    added: int
    changed: int
    removed: int


async def update(root: str | Path, db_path: str | Path) -> UpdateResult:
    """Incrementally reconcile the catalog with the current filesystem.

    New files are inserted, changed files (by size or mtime) updated, and
    vanished files removed.

    Args:
        root: Library root directory to scan.
        db_path: Existing (or new) SQLite catalog path.

    Returns:
        Counts of added, changed and removed tracks.
    """
    added = changed = 0
    async with connect(db_path) as conn:
        await conn.executescript(SCHEMA)
        cursor = await conn.execute("SELECT rel_path, size_bytes, mtime_ns FROM tracks")
        existing = {
            row["rel_path"]: (row["size_bytes"], row["mtime_ns"])
            for row in await cursor.fetchall()
        }
        seen: set[str] = set()
        for meta in iter_tracks(root):
            seen.add(meta.rel_path)
            prev = existing.get(meta.rel_path)
            if prev is None:
                added += 1
            elif prev != (meta.size_bytes, meta.mtime_ns):
                changed += 1
            else:
                continue
            await conn.execute(_INSERT, _row(meta))
        removed = [rel for rel in existing if rel not in seen]
        if removed:
            await conn.executemany(
                "DELETE FROM tracks WHERE rel_path = ?",
                [(rel,) for rel in removed],
            )
        await _write_meta(conn, len(seen))
        await conn.commit()
    return UpdateResult(added=added, changed=changed, removed=len(removed))
