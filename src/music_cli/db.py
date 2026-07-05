"""Small async SQLite helpers shared by the catalog and device databases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


@asynccontextmanager
async def connect(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open an aiosqlite connection with ``Row`` access and sane pragmas.

    Args:
        path: Filesystem path to the SQLite database (created if absent).

    Yields:
        An open connection that is closed automatically on exit.
    """
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        await conn.close()
