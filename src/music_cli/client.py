"""Async HTTP client for talking to a music server.

All downloads stream to a temporary sibling file and are then atomically
renamed into place, so an interrupted transfer never leaves a half-written
track on the SD card.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx


class MusicClient:
    """Thin wrapper over an :class:`httpx.AsyncClient` and a server base URL."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def get_meta(self) -> dict[str, object]:
        """Return the server's ``/catalog/meta`` payload."""
        resp = await self._http.get(f"{self._base}/catalog/meta")
        resp.raise_for_status()
        return resp.json()

    async def download_catalog(self, dest: Path, etag: str | None = None) -> bool:
        """Download ``catalog.db`` to ``dest`` unless the ETag still matches.

        Args:
            dest: Local path to write the snapshot to.
            etag: Previously seen ETag; sent as ``If-None-Match``.

        Returns:
            ``True`` if a fresh copy was written, ``False`` on a ``304``.
        """
        headers = {"If-None-Match": etag} if etag else {}
        async with self._http.stream(
            "GET", f"{self._base}/catalog.db", headers=headers
        ) as resp:
            if resp.status_code == httpx.codes.NOT_MODIFIED:
                return False
            resp.raise_for_status()
            await _stream_to_file(resp, dest)
        return True

    async def download_track(self, rel_path: str, dest: Path) -> int:
        """Download one audio file to ``dest``.

        Args:
            rel_path: Track path relative to the library root (POSIX style).
            dest: Local destination file.

        Returns:
            The number of bytes written.
        """
        url = f"{self._base}/files/{quote(rel_path)}"
        async with self._http.stream("GET", url) as resp:
            resp.raise_for_status()
            return await _stream_to_file(resp, dest)


async def _stream_to_file(resp: httpx.Response, dest: Path) -> int:
    tmp = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            async for chunk in resp.aiter_bytes():
                fh.write(chunk)
                written += len(chunk)
        os.replace(tmp, dest)
    except BaseException:
        # On error or abort (e.g. an un-tick cancelling the download), don't
        # leave a half-written .part behind.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return written


@asynccontextmanager
async def open_client(base_url: str) -> AsyncIterator[MusicClient]:
    """Open a :class:`MusicClient` backed by a real network connection."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        yield MusicClient(base_url, http)
