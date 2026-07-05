# Music CLI

Share a music collection over the LAN and copy tracks onto the kids' SD cards
without doing it song-by-song.

One machine (the **server**) holds the library and exposes it over HTTP. On a
kid's laptop, the **client** downloads a catalog of the collection, browses it,
and copies chosen artists/albums/tracks onto an SD card — starting downloads in
the background (after a short debounce) while browsing continues.

## How it works

* The catalog is a **SQLite snapshot** built from the folder layout
  (`Artist/Album/NN - Title.ext`) — no audio tags are read.
* The client downloads that snapshot and does **all browsing and diffing
  locally**, so the server only has to serve static files. It is therefore a
  deliberately thin FastAPI app: `/catalog/meta` (freshness), `/catalog.db`
  (the snapshot, with `ETag`/`304`) and `/files/...` (audio, with HTTP range
  requests and path-traversal safety via `StaticFiles`).
* A per-card device database (`.music_cli.db`) records what has been copied so
  re-syncs only transfer what is new or changed.

## Usage

On the server:

```bash
music catalog build  --root /music --db /music/catalog.db   # scan the library
music catalog update --root /music --db /music/catalog.db   # incremental refresh
music serve          --root /music --db /music/catalog.db   # http://0.0.0.0:8000
```

On a kid's machine (SD card mounted at `/media/sdcard`):

```bash
music browse --server http://server:8000 --dest /media/sdcard          # interactive TUI
music sync   --server http://server:8000 --dest /media/sdcard --artist "Queen"
music status --server http://server:8000 --dest /media/sdcard
```

In `browse`, navigate `Artist → Album → Track` (albums, album-less *singles* and
a `(loose tracks)` group all appear). <kbd>Enter</kbd> folds/unfolds a node;
<kbd>Space</kbd> toggles its checkbox (`☐`→`☑`). Ticking an artist or album ticks
all its tracks. Ticked tracks download in the background a few seconds later (a
mis-click un-ticked in time never downloads); progress shows at the bottom.

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

### Quality gates

| Command | What it checks |
|---|---|
| `uv run ruff format --check` | Formatting |
| `uv run ruff check` | Linting |
| `uv run ty check` | Static type errors |
| `uv run pytest` | Unit + integration tests, coverage ≥ 85% |
| `uv run pytest tests/e2e` | End-to-end tests (spawns real server + client subprocesses) |
| `uv run pytest tests/contract` | Pact consumer generation + provider verification |

> **Toolchain:** targets Python **3.14** (pinned to 3.14.6 in `.python-version`).
> Fetching a stable 3.14 needs a recent `uv`; if `uv python install 3.14.6`
> reports only alpha builds, update `uv` first (`curl -LsSf
> https://astral.sh/uv/install.sh | sh`).

### Layout

```
music_cli/
├── src/music_cli/
│   ├── catalog.py   # folder→metadata scan, build/update, read/query
│   ├── db.py        # async sqlite connection helper
│   ├── server.py    # thin FastAPI app (meta + snapshot + static files)
│   ├── client.py    # httpx client (meta, snapshot, track download)
│   ├── device.py    # per-card device DB + copy diff
│   ├── sync.py      # non-interactive sync/status orchestration
│   ├── syncq.py     # debounced background download queue
│   ├── tui.py       # Textual browse UI
│   └── cli.py       # argparse entry point
└── tests/
    ├── unit/          # pure functions, no I/O
    ├── integration/   # in-process ASGI + tmp sqlite
    ├── e2e/           # real server + client subprocesses (opt-in)
    └── contract/      # Pact consumer generation + provider verification (opt-in)
```
