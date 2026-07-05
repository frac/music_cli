"""Per-card device database and the snapshot/diff logic.

Two hidden files live at the SD-card root so state travels with the card:

* ``.music_cli.db``          — record of what has been copied here.
* ``.music_cli.catalog.db``  — cached copy of the server catalog snapshot.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path

from music_cli.catalog import CatalogTrack
from music_cli.db import connect

DEVICE_DB_NAME = ".music_cli.db"
CATALOG_CACHE_NAME = ".music_cli.catalog.db"

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
