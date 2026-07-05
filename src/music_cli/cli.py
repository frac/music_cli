"""Command-line entry point for the music CLI.

Subcommands:
    catalog build|update   Scan a library and (re)build the SQLite catalog.
    serve                  Serve the catalog + audio files over HTTP.
    sync                   Non-interactively copy tracks to an SD card.
    status                 Show what is on the card vs. available on the server.
    browse                 Interactive TUI to browse and background-copy tracks.

Feature modules are imported lazily inside each handler so that, for example,
``music catalog build`` does not need the TUI or HTTP-client dependencies loaded.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from music_cli import __version__

DEFAULT_DB = "catalog.db"


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="music", description=__doc__)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    catalog = sub.add_parser("catalog", help="build or update the catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    for name in ("build", "update"):
        p = catalog_sub.add_parser(name, help=f"{name} the catalog")
        p.add_argument("--root", type=Path, required=True, help="library root")
        p.add_argument("--db", type=Path, default=Path(DEFAULT_DB))

    serve = sub.add_parser("serve", help="serve catalog + files over HTTP")
    serve.add_argument("--root", type=Path, required=True, help="library root")
    serve.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--refresh-minutes",
        type=float,
        default=10.0,
        help="rescan the library this often (0 disables)",
    )

    for name, help_ in (
        ("sync", "copy tracks to the SD card"),
        ("status", "show on-card vs. available diff"),
        ("browse", "interactive TUI"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument(
            "--server",
            help="base URL of the server (remembered after first use)",
        )
        p.add_argument(
            "--dest",
            type=Path,
            help="destination dir (default: detected card or remembered dir)",
        )
        if name == "sync":
            p.add_argument("--artist", help="only this artist")
            p.add_argument("--album", help="only this album")

    return parser


def _resolve_target(args: argparse.Namespace) -> tuple[str, Path]:
    """Resolve server+dest from flags/card/config; remember on success."""
    from music_cli import config

    try:
        server, dest = config.resolve(args.server, args.dest)
    except config.ResolutionError as exc:
        raise SystemExit(f"music: {exc}") from exc
    # Remember the server always; remember dest only when explicitly given,
    # so a transient auto-detected card never overwrites e.g. a phone dir.
    saved = config.load()
    config.save(
        config.Settings(
            server=server,
            dest=str(args.dest) if args.dest else saved.dest,
        )
    )
    return server, dest


def _cmd_catalog(args: argparse.Namespace) -> int:
    from music_cli import catalog

    if args.catalog_command == "build":
        count = asyncio.run(catalog.build(args.root, args.db))
        print(f"Built catalog: {count} tracks → {args.db}")
    else:
        result = asyncio.run(catalog.update(args.root, args.db))
        print(
            f"Updated catalog: +{result.added} ~{result.changed} "
            f"-{result.removed} → {args.db}"
        )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from music_cli.server import create_app

    refresh = args.refresh_minutes * 60 if args.refresh_minutes > 0 else None
    app = create_app(args.root, args.db, refresh_seconds=refresh)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from music_cli.sync import run_sync

    server, dest = _resolve_target(args)
    copied = asyncio.run(run_sync(server, dest, artist=args.artist, album=args.album))
    print(f"Copied {copied} track(s) → {dest}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from music_cli.sync import run_status

    server, dest = _resolve_target(args)
    asyncio.run(run_status(server, dest))
    return 0


def _cmd_browse(args: argparse.Namespace) -> int:
    from music_cli.tui import run_browse

    server, dest = _resolve_target(args)
    run_browse(server, dest)
    return 0


_DISPATCH = {
    "catalog": _cmd_catalog,
    "serve": _cmd_serve,
    "sync": _cmd_sync,
    "status": _cmd_status,
    "browse": _cmd_browse,
}


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected subcommand.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
