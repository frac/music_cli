"""Interactive Textual TUI for browsing the catalog and background-copying.

Navigation tree: ``Artist → Album → Track``, plus album-less *singles* directly
under an artist and a ``(loose tracks)`` group for files with no artist — so
every audio file in the library is reachable.

Controls:
    ↑/↓     move
    Enter   fold / unfold a node (no selection)
    Space   toggle the checkbox. ``☑`` on card (or queued), ``☐`` not,
            ``◐`` some-but-not-all tracks of a group. Space on a partial
            group completes it; on a full group it removes everything.
    /       type-to-search (filters artists by name, album or track title);
            Esc clears the filter.
    r       retry failed downloads
    q       quit

Changes take effect after a short grace window (see ``syncq``), and the footer
shows queue progress plus the card's remaining free space.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar
from typing import Protocol
from typing import cast

from rich.text import Text
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer
from textual.widgets import Header
from textual.widgets import Input
from textual.widgets import Static
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from music_cli import catalog
from music_cli.catalog import CatalogTrack
from music_cli.device import card_path
from music_cli.device import load_copied
from music_cli.device import record_copied
from music_cli.device import remove_copied
from music_cli.syncq import Downloader
from music_cli.syncq import DownloadItem
from music_cli.syncq import SyncQueue
from music_cli.syncq import TrackState


class TrackSource(Protocol):
    """The slice of :class:`~music_cli.client.MusicClient` a downloader needs."""

    async def download_track(self, rel_path: str, dest: Path) -> int: ...


def track_item(track: CatalogTrack) -> DownloadItem:
    """Wrap a catalog track as a queue item keyed by its relative path."""
    return DownloadItem(key=track.rel_path, payload=track)


def checkbox_markup(base: str, checked: bool | None) -> str:
    """Return a node label with a checkbox glyph (``None`` = partial)."""
    if checked is None:
        return f"[yellow]◐[/] {base}"
    return f"[green]☑[/] {base}" if checked else f"☐ {base}"


def human_size(n: int) -> str:
    """Format a byte count for humans (e.g. ``1.4 GB``)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - unreachable


def format_status(counts: dict[str, int]) -> str:
    """Render queue counts as a compact one-line progress string."""
    return (
        f"⧗ {counts.get('pending', 0)} pending   "
        f"⇩ {counts.get('downloading', 0)} copying   "
        f"⌫ {counts.get('removing', 0)} removing   "
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
        target = card_path(dest, track.rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        size = await client.download_track(track.rel_path, target)
        await record_copied(dest, track.rel_path, size, track.mtime_ns, generated_at)

    return _download


class CheckTree(Tree):
    """A tree where Space toggles a checkbox and Enter folds/unfolds."""

    BINDINGS: ClassVar = [
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

    CSS = """
    #status { dock: bottom; height: 1; color: $accent; }
    #search { dock: top; display: none; }
    #search.visible { display: block; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("r", "retry", "Retry failed"),
        Binding("escape", "clear_search", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        cache: str | Path,
        downloader: Downloader,
        dest: str | Path,
        debounce: float = 5.0,
    ) -> None:
        super().__init__()
        self._cache = Path(cache)
        self._dest = Path(dest)
        self._filter = ""
        self._reserved: dict[str, int] = {}  # queued-to-card bytes by rel_path
        self._queue = SyncQueue(
            download=downloader,
            remove=self._remove_track,
            debounce=debounce,
            on_event=self._on_event,
        )
        self._tree: CheckTree | None = None
        self._status: Static | None = None
        self._search: Input | None = None

    async def _remove_track(self, rel_path: str) -> None:
        await remove_copied(self._dest, rel_path)

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._search = Input(placeholder="search artist / album / title…", id="search")
        yield self._search
        self._tree = CheckTree("Music")
        yield self._tree
        self._status = Static(format_status(self._queue.counts()), id="status")
        yield self._status
        yield Footer()

    async def on_mount(self) -> None:
        assert self._tree is not None
        self._queue.seed_present(await load_copied(self._dest))
        self.run_worker(self._queue.run(), exclusive=False)
        await self._populate()
        self._refresh()
        self._tree.focus()

    async def _populate(self) -> None:
        """(Re)build the artist level of the tree for the current filter."""
        assert self._tree is not None
        root = self._tree.root
        root.remove_children()
        artists = await (
            catalog.search_artists(self._cache, self._filter)
            if self._filter
            else catalog.list_artists(self._cache)
        )
        for artist in artists:
            data = {"kind": "artist", "artist": artist, "base": artist}
            data["checked"] = await self._group_state(data)
            self._add(root, data)
        if await catalog.list_loose(self._cache, matching=self._filter or None):
            loose = {"kind": "loose", "base": "(loose tracks)"}
            loose["checked"] = await self._group_state(loose)
            self._add(root, loose)
        root.expand()

    # -- checked-state model ---------------------------------------------------

    def _is_checked(self, rel_path: str) -> bool:
        """A track is ticked if its intent is to be on the card."""
        return self._queue.wants_present(rel_path)

    async def _group_state(self, data: dict) -> bool | None:
        """``True`` all on card, ``False`` none, ``None`` partial."""
        tracks = await self._tracks_for(data)
        if not tracks:
            return False
        flags = [self._is_checked(t.rel_path) for t in tracks]
        if all(flags):
            return True
        return None if any(flags) else False

    # -- node construction -------------------------------------------------

    def _add(self, parent: TreeNode, data: dict, *, leaf: bool = False) -> TreeNode:
        data.setdefault("checked", False)
        label = Text.from_markup(checkbox_markup(data["base"], data["checked"]))
        return (
            parent.add_leaf(label, data=data) if leaf else parent.add(label, data=data)
        )

    def _add_track(self, parent: TreeNode, track: CatalogTrack) -> None:
        base = f"{track.track_no or '--'}  {track.title}"
        self._add(
            parent,
            {
                "kind": "track",
                "track": track,
                "base": base,
                "checked": self._is_checked(track.rel_path),
            },
            leaf=True,
        )

    async def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data or {}
        if node.children or data.get("kind") not in ("artist", "album", "loose"):
            return
        if data["kind"] == "artist":
            for album in await catalog.list_albums(self._cache, data["artist"]):
                album_data = {
                    "kind": "album",
                    "artist": data["artist"],
                    "album": album,
                    "base": album,
                }
                album_data["checked"] = await self._group_state(album_data)
                self._add(node, album_data)
            for track in await catalog.list_singles(self._cache, data["artist"]):
                self._add_track(node, track)
        elif data["kind"] == "album":
            tracks = await catalog.query_tracks(
                self._cache, artist=data["artist"], album=data["album"]
            )
            for track in tracks:
                self._add_track(node, track)
        else:  # loose
            for track in await catalog.list_loose(self._cache):
                self._add_track(node, track)

    # -- selection ---------------------------------------------------------

    async def on_check_tree_checked(self, message: CheckTree.Checked) -> None:
        node = message.node
        data = node.data
        if not data:
            return
        # Partial or empty → complete the selection; full → remove everything.
        new = data.get("checked") is not True
        skipped = 0
        for track in await self._tracks_for(data):
            if new:
                if self._is_checked(track.rel_path):
                    continue
                if not self._fits(track.size_bytes):
                    skipped += 1
                    continue
                self._reserved[track.rel_path] = track.size_bytes
                self._queue.select(track_item(track))
            else:
                self._reserved.pop(track.rel_path, None)
                self._queue.deselect(track.rel_path)
        self._set_subtree_checked(node, new)
        await self._refresh_ancestors(node)
        if skipped:
            self.notify(
                f"Card is full: skipped {skipped} track(s).",
                severity="warning",
            )
        self._refresh()

    def _free_bytes(self) -> int | None:
        try:
            free = shutil.disk_usage(self._dest).free
        except OSError:
            return None
        return free - sum(self._reserved.values())

    def _fits(self, size: int) -> bool:
        free = self._free_bytes()
        return free is None or size <= free

    def _set_subtree_checked(self, node: TreeNode, checked: bool) -> None:
        data = node.data
        if data is not None and "checked" in data:
            data["checked"] = checked
            node.set_label(Text.from_markup(checkbox_markup(data["base"], checked)))
        for child in node.children:
            self._set_subtree_checked(child, checked)

    async def _refresh_ancestors(self, node: TreeNode) -> None:
        parent = node.parent
        while parent is not None:
            data = parent.data
            if data and data.get("kind") in ("artist", "album", "loose"):
                state = await self._group_state(data)
                data["checked"] = state
                parent.set_label(Text.from_markup(checkbox_markup(data["base"], state)))
            parent = parent.parent

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

    # -- search --------------------------------------------------------------

    def action_search(self) -> None:
        assert self._search is not None
        self._search.add_class("visible")
        self._search.focus()

    async def action_clear_search(self) -> None:
        assert self._search is not None and self._tree is not None
        if not self._search.has_class("visible") and not self._filter:
            return
        self._search.value = ""
        self._search.remove_class("visible")
        self._filter = ""
        await self._populate()
        self._tree.focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        await self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        assert self._search is not None and self._tree is not None
        self._search.remove_class("visible")
        self._tree.focus()

    # -- retry ---------------------------------------------------------------

    def action_retry(self) -> None:
        failed = self._queue.failed_keys()
        if not failed:
            self.notify("No failed downloads.")
            return
        names = ", ".join(self._queue.title_of(k) for k in failed[:3])
        more = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        count = self._queue.retry_failed()
        self.notify(f"Retrying {count}: {names}{more}")
        self._refresh()

    # -- progress + lifecycle ---------------------------------------------

    def _on_event(self, key: str, state: TrackState) -> None:
        if state in (TrackState.PRESENT, TrackState.FAILED):
            self._reserved.pop(key, None)
        self.call_later(self._refresh)

    def _refresh(self) -> None:
        if self._status is None:
            return
        line = format_status(self._queue.counts())
        free = self._free_bytes()
        if free is not None:
            line += f"   💾 {human_size(max(free, 0))} free"
        self._status.update(line)

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
            cache=cache,
            downloader=make_downloader(client, dest, generated_at),
            dest=dest,
        )
        await app.run_async()
