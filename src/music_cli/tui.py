"""Interactive Textual TUI for browsing the catalog and background-copying.

Navigation tree: ``Artist → Album → Track``, plus album-less *singles* directly
under an artist and a ``(loose tracks)`` group for files with no artist — so
every audio file in the library is reachable.

Controls:
    ↑/↓     move
    Enter   fold / unfold a node (no selection)
    Space   toggle the checkbox (☐ → ☑); selecting an artist or album selects
            all of its tracks. Ticked tracks download in the background after a
            short debounce; progress shows in the footer.
    q       quit

Only the pure glue helpers (:func:`checkbox_markup`, :func:`track_item`,
:func:`format_status`, :func:`make_downloader`) are unit-tested directly; the
widget classes are exercised by a Textual ``Pilot`` test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from typing import cast

from rich.text import Text
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer
from textual.widgets import Header
from textual.widgets import Static
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from music_cli import catalog
from music_cli.catalog import CatalogTrack
from music_cli.device import record_copied
from music_cli.syncq import Downloader
from music_cli.syncq import DownloadItem
from music_cli.syncq import DownloadQueue


class TrackSource(Protocol):
    """The slice of :class:`~music_cli.client.MusicClient` a downloader needs."""

    async def download_track(self, rel_path: str, dest: Path) -> int: ...


def track_item(track: CatalogTrack) -> DownloadItem:
    """Wrap a catalog track as a queue item keyed by its relative path."""
    return DownloadItem(key=track.rel_path, payload=track)


def checkbox_markup(base: str, checked: bool) -> str:
    """Return a node label with a leading checkbox glyph (Rich markup)."""
    return f"[green]☑[/] {base}" if checked else f"☐ {base}"


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
    """Build a queue downloader that copies a track and records it on the card."""
    dest = Path(dest)

    async def _download(item: DownloadItem) -> None:
        track = cast(CatalogTrack, item.payload)
        target = dest / track.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        size = await client.download_track(track.rel_path, target)
        await record_copied(dest, track.rel_path, size, track.mtime_ns, generated_at)

    return _download


class CheckTree(Tree):
    """A tree where Space toggles a checkbox and Enter folds/unfolds."""

    BINDINGS = [  # noqa: RUF012 (Textual reads BINDINGS off the class)
        Binding("space", "check", "Select"),
        Binding("enter", "fold", "Fold/unfold"),
    ]

    class Checked(Message):
        """Posted when the user toggles a node's checkbox."""

        def __init__(self, node: TreeNode) -> None:
            self.node = node
            super().__init__()

    def action_check(self) -> None:
        if self.cursor_node is not None:
            self.post_message(self.Checked(self.cursor_node))

    def action_fold(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.toggle()


class BrowseApp(App):
    """Browse the catalog and queue tracks for background copying."""

    CSS = "#status { dock: bottom; height: 1; color: $accent; }"
    BINDINGS = [Binding("q", "quit", "Quit")]  # noqa: RUF012

    def __init__(
        self, *, cache: str | Path, downloader: Downloader, debounce: float = 5.0
    ) -> None:
        super().__init__()
        self._cache = Path(cache)
        self._queue = DownloadQueue(
            downloader=downloader, debounce=debounce, on_event=self._on_event
        )
        self._tree: CheckTree | None = None
        self._status: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._tree = CheckTree("Music")
        yield self._tree
        self._status = Static(format_status(self._queue.counts()), id="status")
        yield self._status
        yield Footer()

    async def on_mount(self) -> None:
        assert self._tree is not None
        self.run_worker(self._queue.run(), exclusive=False)
        root = self._tree.root
        for artist in await catalog.list_artists(self._cache):
            self._add(root, {"kind": "artist", "artist": artist, "base": artist})
        if await catalog.list_loose(self._cache):
            self._add(root, {"kind": "loose", "base": "(loose tracks)"})
        root.expand()
        self._tree.focus()

    # -- node construction -------------------------------------------------

    def _add(self, parent: TreeNode, data: dict, *, leaf: bool = False) -> TreeNode:
        data.setdefault("checked", False)
        label = Text.from_markup(checkbox_markup(data["base"], data["checked"]))
        return (
            parent.add_leaf(label, data=data) if leaf else parent.add(label, data=data)
        )

    def _add_track(self, parent: TreeNode, track: CatalogTrack, checked: bool) -> None:
        base = f"{track.track_no or '--'}  {track.title}"
        self._add(
            parent,
            {"kind": "track", "track": track, "base": base, "checked": checked},
            leaf=True,
        )

    async def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data or {}
        if node.children or data.get("kind") not in ("artist", "album", "loose"):
            return
        checked = data.get("checked", False)
        if data["kind"] == "artist":
            for album in await catalog.list_albums(self._cache, data["artist"]):
                self._add(
                    node,
                    {
                        "kind": "album",
                        "artist": data["artist"],
                        "album": album,
                        "base": album,
                        "checked": checked,
                    },
                )
            for track in await catalog.list_singles(self._cache, data["artist"]):
                self._add_track(node, track, checked)
        elif data["kind"] == "album":
            tracks = await catalog.query_tracks(
                self._cache, artist=data["artist"], album=data["album"]
            )
            for track in tracks:
                self._add_track(node, track, checked)
        else:  # loose
            for track in await catalog.list_loose(self._cache):
                self._add_track(node, track, checked)

    # -- selection ---------------------------------------------------------

    async def on_check_tree_checked(self, message: CheckTree.Checked) -> None:
        node = message.node
        data = node.data
        if not data:
            return
        new = not data.get("checked", False)
        for track in await self._tracks_for(data):
            if new:
                self._queue.select(track_item(track))
            else:
                self._queue.deselect(track.rel_path)
        self._set_subtree_checked(node, new)
        self._refresh()

    def _set_subtree_checked(self, node: TreeNode, checked: bool) -> None:
        data = node.data
        if data is not None and "checked" in data:
            data["checked"] = checked
            node.set_label(Text.from_markup(checkbox_markup(data["base"], checked)))
        for child in node.children:
            self._set_subtree_checked(child, checked)

    async def _tracks_for(self, data: dict) -> list[CatalogTrack]:
        kind = data.get("kind")
        if kind == "track":
            return [data["track"]]
        if kind == "album":
            return await catalog.query_tracks(
                self._cache, artist=data["artist"], album=data["album"]
            )
        if kind == "artist":
            return await catalog.query_tracks(self._cache, artist=data["artist"])
        if kind == "loose":
            return await catalog.list_loose(self._cache)
        return []

    # -- progress + lifecycle ---------------------------------------------

    def _on_event(self, key: str, state: object) -> None:
        self.call_later(self._refresh)

    def _refresh(self) -> None:
        if self._status is not None:
            self._status.update(format_status(self._queue.counts()))

    async def on_unmount(self) -> None:
        self._queue.stop()


def run_browse(server: str, dest: str | Path) -> None:  # pragma: no cover
    """Launch the interactive browser against ``server`` copying to ``dest``."""
    import asyncio

    asyncio.run(_main(server, dest))


async def _main(server: str, dest: str | Path) -> None:  # pragma: no cover
    from music_cli.client import open_client
    from music_cli.sync import refresh_catalog

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    async with open_client(server) as client:
        cache = await refresh_catalog(client, dest)
        generated_at = (await catalog.read_meta(cache)).get("generated_at")
        app = BrowseApp(
            cache=cache, downloader=make_downloader(client, dest, generated_at)
        )
        await app.run_async()
