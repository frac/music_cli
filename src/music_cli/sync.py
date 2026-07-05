"""Non-interactive sync + status orchestration.

Each operation is split into a *core* that takes an already-open
:class:`~music_cli.client.MusicClient` (so it can be driven against an in-process
ASGI app in tests) and a thin *wrapper* that opens a real network client.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from music_cli import catalog
from music_cli.client import MusicClient
from music_cli.client import open_client
from music_cli.device import catalog_cache_path
from music_cli.device import load_copied
from music_cli.device import needs_copy
from music_cli.device import record_copied


async def refresh_catalog(client: MusicClient, dest: str | Path) -> Path:
    """Ensure the card has an up-to-date cached catalog snapshot.

    Uses a stored ETag to skip the download when the server catalog is
    unchanged.

    Returns:
        Path to the cached ``catalog.db`` on the card.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    cache = catalog_cache_path(dest)
    etag_file = cache.with_name(cache.name + ".etag")

    prior = (
        etag_file.read_text().strip() if cache.exists() and etag_file.exists() else None
    )
    meta = await client.get_meta()
    server_etag = str(meta.get("etag") or "")
    if cache.exists() and prior == server_etag:
        return cache

    downloaded = await client.download_catalog(cache, etag=prior)
    if downloaded and server_etag:
        etag_file.write_text(server_etag)
    return cache


@dataclass(frozen=True, slots=True)
class StatusSummary:
    """Counts describing a card relative to the server catalog."""

    on_card: int
    available: int
    to_copy: int


async def sync_tracks(
    client: MusicClient,
    dest: str | Path,
    *,
    artist: str | None = None,
    album: str | None = None,
) -> int:
    """Copy every matching, not-yet-present track to the card.

    Returns:
        The number of tracks actually copied.
    """
    dest = Path(dest)
    cache = await refresh_catalog(client, dest)
    generated_at = (await catalog.read_meta(cache)).get("generated_at")
    tracks = await catalog.query_tracks(cache, artist=artist, album=album)
    copied = await load_copied(dest)

    count = 0
    for track in tracks:
        if not needs_copy(track, copied):
            continue
        target = dest / track.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        size = await client.download_track(track.rel_path, target)
        await record_copied(dest, track.rel_path, size, track.mtime_ns, generated_at)
        count += 1
    return count


async def status_summary(client: MusicClient, dest: str | Path) -> StatusSummary:
    """Compute how the card compares to the server catalog."""
    dest = Path(dest)
    cache = await refresh_catalog(client, dest)
    tracks = await catalog.query_tracks(cache)
    copied = await load_copied(dest)
    to_copy = sum(1 for t in tracks if needs_copy(t, copied))
    return StatusSummary(on_card=len(copied), available=len(tracks), to_copy=to_copy)


async def run_sync(
    server: str,
    dest: str | Path,
    *,
    artist: str | None = None,
    album: str | None = None,
) -> int:
    """Open a network client and run :func:`sync_tracks`."""
    async with open_client(server) as client:
        return await sync_tracks(client, dest, artist=artist, album=album)


async def run_status(server: str, dest: str | Path) -> StatusSummary:
    """Open a network client, print and return a :func:`status_summary`."""
    async with open_client(server) as client:
        summary = await status_summary(client, dest)
    print(
        f"{summary.on_card} on card · {summary.available} available · "
        f"{summary.to_copy} to copy"
    )
    return summary
