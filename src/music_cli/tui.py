"""Interactive Textual TUI for browsing the catalog and background-copying.

The kid navigates an ``Artist → Album → Track`` tree. Selecting any node
enqueues its track(s) into a :class:`~music_cli.syncq.DownloadQueue`, which
starts copying to the SD card after the debounce window while browsing
continues. A footer shows live progress.

Only the small, pure glue helpers (:func:`make_downloader`,
:func:`format_status`, :func:`track_item`) are unit-tested; the Textual widget
plumbing is excluded from coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Protocol
from typing import cast

from music_cli.catalog import CatalogTrack
from music_cli.client import MusicClient
from music_cli.device import record_copied
from music_cli.syncq import Downloader
from music_cli.syncq import DownloadItem
from music_cli.syncq import DownloadState


class TrackSource(Protocol):
    """The slice of :class:`~music_cli.client.MusicClient` a downloader needs."""

    async def download_track(self, rel_path: str, dest: Path) -> int: ...


def track_item(track: CatalogTrack) -> DownloadItem:
    """Wrap a catalog track as a queue item keyed by its relative path."""
    return DownloadItem(key=track.rel_path, payload=track)


def format_status(counts: dict[str, int]) -> str:
    """Render queue counts as a compact one-line progress string."""
    return (
        f"⧗ {counts.get('pending', 0)} pending   "
        f"⇩ {counts.get('downloading', 0)} copying   "
        f"✓ {counts.get('done', 0)} done   "
        f"✗ {counts.get('failed', 0)} failed"
    )


def make_downloader(
    client: TrackSource, dest: str | Path, generated_at: str | None
) -> Downloader:
    """Build a queue downloader that copies a track and records it on the card.

    Args:
        client: Connected music client.
        dest: SD-card destination directory.
        generated_at: Catalog ``generated_at`` stamp to store alongside the copy.

    Returns:
        An async ``downloader(item)`` for :class:`~music_cli.syncq.DownloadQueue`.
    """
    dest = Path(dest)

    async def _download(item: DownloadItem) -> None:
        track = cast(CatalogTrack, item.payload)
        target = dest / track.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        size = await client.download_track(track.rel_path, target)
        await record_copied(dest, track.rel_path, size, track.mtime_ns, generated_at)

    return _download


def run_browse(server: str, dest: str | Path) -> None:  # pragma: no cover
    """Launch the interactive browser against ``server`` copying to ``dest``."""
    import asyncio

    asyncio.run(_main(server, dest))


async def _main(server: str, dest: str | Path) -> None:  # pragma: no cover
    from music_cli import catalog
    from music_cli.client import open_client
    from music_cli.sync import refresh_catalog

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    async with open_client(server) as client:
        cache = await refresh_catalog(client, dest)
        generated_at = (await catalog.read_meta(cache)).get("generated_at")
        app = BrowseApp(client, cache, dest, generated_at)
        await app.run_async()


class BrowseApp:  # pragma: no cover
    """Textual application; imported lazily so tests need no terminal."""

    def __init__(
        self,
        client: MusicClient,
        cache: Path,
        dest: Path,
        generated_at: str | None,
    ) -> None:
        from textual.app import App
        from textual.widgets import Footer
        from textual.widgets import Header
        from textual.widgets import Static
        from textual.widgets import Tree

        from music_cli import catalog
        from music_cli.syncq import DownloadQueue

        self._catalog = catalog
        self._cache = cache
        self._tree: Any = None  # Textual Tree, created in compose()
        self._status: Any = None  # Textual Static, created in compose()
        self._queue = DownloadQueue(
            downloader=make_downloader(client, dest, generated_at),
            on_event=self._on_queue_event,
        )

        outer = self

        class _App(App):
            BINDINGS: ClassVar = [("q", "quit", "Quit")]
            CSS = "#status { dock: bottom; height: 1; color: $accent; }"

            def compose(self):
                yield Header(show_clock=True)
                outer._tree = Tree("Artists")
                yield outer._tree
                outer._status = Static(
                    format_status(outer._queue.counts()), id="status"
                )
                yield outer._status
                yield Footer()

            async def on_mount(self) -> None:
                self.run_worker(outer._queue.run(), exclusive=False)
                for artist in await outer._catalog.list_artists(outer._cache):
                    outer._tree.root.add(
                        artist, data={"type": "artist", "artist": artist}
                    )
                outer._tree.root.expand()

            async def on_tree_node_expanded(self, event) -> None:
                await outer._expand(event.node)

            async def on_tree_node_selected(self, event) -> None:
                for track in await outer._tracks_for(event.node.data or {}):
                    outer._queue.select(track_item(track))

            async def on_unmount(self) -> None:
                outer._queue.stop()

        self._app = _App()

    async def _expand(self, node) -> None:
        data = node.data or {}
        if node.children:
            return
        if data.get("type") == "artist":
            for album in await self._catalog.list_albums(self._cache, data["artist"]):
                node.add(
                    album,
                    data={
                        "type": "album",
                        "artist": data["artist"],
                        "album": album,
                    },
                )
        elif data.get("type") == "album":
            for track in await self._catalog.query_tracks(
                self._cache, artist=data["artist"], album=data["album"]
            ):
                node.add_leaf(
                    f"{track.track_no or '--'}  {track.title}",
                    data={"type": "track", "track": track},
                )

    async def _tracks_for(self, data: dict) -> list[CatalogTrack]:
        kind = data.get("type")
        if kind == "track":
            return [data["track"]]
        if kind == "album":
            return await self._catalog.query_tracks(
                self._cache, artist=data["artist"], album=data["album"]
            )
        if kind == "artist":
            return await self._catalog.query_tracks(self._cache, artist=data["artist"])
        return []

    def _on_queue_event(self, key: str, state: DownloadState) -> None:
        self._app.call_later(self._refresh)

    def _refresh(self) -> None:
        self._status.update(format_status(self._queue.counts()))

    async def run_async(self) -> None:
        await self._app.run_async()
