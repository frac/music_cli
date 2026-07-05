"""Per-card device database and the snapshot/diff logic.

Two hidden files live at the SD-card root so state travels with the card:

* ``.music_cli.db``          — record of what has been copied here.
* ``.music_cli.catalog.db``  — cached copy of the server catalog snapshot.
"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import datetime
from pathlib import Path

from music_cli.catalog import CatalogTrack
from music_cli.db import connect

DEVICE_DB_NAME = ".music_cli.db"
CATALOG_CACHE_NAME = ".music_cli.catalog.db"

#: Characters FAT32/exFAT/NTFS refuse in filenames (plus control chars).
_FAT_ILLEGAL = re.compile(r'[<>:"\\|?*\x00-\x1f]')

#: Windows-reserved device names (case-insensitive, extension ignored).
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _safe_segment(name: str) -> str:
    """Make one path segment legal on FAT32/exFAT/NTFS."""
    cleaned = _FAT_ILLEGAL.sub("_", name).rstrip(" .")
    if not cleaned:
        cleaned = "_"
    stem = cleaned.split(".", 1)[0].lower()
    if stem in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_rel_path(rel_path: str) -> str:
    """Sanitize every segment of a catalog rel_path for SD-card filesystems.

    The catalog key stays the *original* rel_path; only the on-card location
    uses the sanitized form, so a library name like ``AC/DC: Live.mp3`` (legal
    on ext4) still copies onto a FAT32 card.
    """
    return "/".join(_safe_segment(seg) for seg in rel_path.split("/") if seg)


def card_path(dest: str | Path, rel_path: str) -> Path:
    """Return the on-card path for a catalog track (FAT-safe)."""
    return Path(dest) / safe_rel_path(rel_path)


DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS copied (
    rel_path             TEXT PRIMARY KEY,
    size_bytes           INTEGER NOT NULL,
    mtime_ns             INTEGER,
    copied_at            TEXT NOT NULL,
    catalog_generated_at TEXT
);
"""


def device_db_path(dest: str | Path) -> Path:
    """Return the device DB path for a card directory."""
    return Path(dest) / DEVICE_DB_NAME


def catalog_cache_path(dest: str | Path) -> Path:
    """Return the cached catalog snapshot path for a card directory."""
    return Path(dest) / CATALOG_CACHE_NAME


async def load_copied(dest: str | Path) -> dict[str, int]:
    """Return ``{rel_path: size_bytes}`` for everything recorded on the card."""
    path = device_db_path(dest)
    if not path.exists():
        return {}
    async with connect(path) as conn:
        await conn.executescript(DEVICE_SCHEMA)
        cursor = await conn.execute("SELECT rel_path, size_bytes FROM copied")
        return {row["rel_path"]: row["size_bytes"] for row in await cursor.fetchall()}


async def record_copied(
    dest: str | Path,
    rel_path: str,
    size_bytes: int,
    mtime_ns: int | None,
    catalog_generated_at: str | None,
) -> None:
    """Record (or update) a successfully copied track in the device DB."""
    async with connect(device_db_path(dest)) as conn:
        await conn.executescript(DEVICE_SCHEMA)
        await conn.execute(
            "INSERT INTO copied "
            "(rel_path, size_bytes, mtime_ns, copied_at, catalog_generated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET "
            "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
            "copied_at=excluded.copied_at, "
            "catalog_generated_at=excluded.catalog_generated_at",
            (
                rel_path,
                size_bytes,
                mtime_ns,
                datetime.now(UTC).isoformat(),
                catalog_generated_at,
            ),
        )
        await conn.commit()


def needs_copy(track: CatalogTrack, copied: dict[str, int]) -> bool:
    """Return whether ``track`` must be (re)copied given the card's state.

    A track is copied when it is absent from the card, or present with a
    different size (i.e. the library file changed).
    """
    previous = copied.get(track.rel_path)
    return previous is None or previous != track.size_bytes


def _prune_empty_dirs(directory: Path, root: Path) -> None:
    """Remove now-empty directories from ``directory`` up towards ``root``.

    ``Path.rmdir`` only removes empty directories, so this can never delete a
    file; it stops at ``root`` or the first non-empty parent.
    """
    directory = directory.resolve()
    root = root.resolve()
    while directory != root and directory.is_relative_to(root):
        try:
            directory.rmdir()
        except OSError:
            break
        directory = directory.parent


async def remove_copied(dest: str | Path, rel_path: str) -> bool:
    """Delete a track we previously copied to the card — and only such a track.

    Safety rules (all must hold for the file to be unlinked):

    * ``rel_path`` is **recorded in this card's device DB** — i.e. we put it
      here. Files we never copied are never touched.
    * The target is a real, non-symlink file that resolves to a path **inside**
      ``dest``. A symlink or ``..`` escape pointing at music elsewhere is left
      alone.

    The device-DB record is always cleared (the track is no longer "on the
    card"), even if the file was already gone or was refused deletion.

    Args:
        dest: SD-card directory.
        rel_path: Track path relative to the library root.

    Returns:
        ``True`` if a file was actually deleted.
    """
    dest = Path(dest).resolve()
    db = device_db_path(dest)
    if not db.exists():
        return False

    async with connect(db) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM copied WHERE rel_path = ?", (rel_path,)
        )
        if await cursor.fetchone() is None:
            return False  # we have no record of putting this here — hands off

        target = card_path(dest, rel_path)
        deleted = False
        try:
            inside = target.resolve().is_relative_to(dest)
        except OSError:
            inside = False
        if inside and target.is_file() and not target.is_symlink():
            target.unlink()
            deleted = True
            _prune_empty_dirs(target.parent, dest)

        await conn.execute("DELETE FROM copied WHERE rel_path = ?", (rel_path,))
        await conn.commit()
    return deleted
