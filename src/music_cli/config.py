"""Remembered settings and SD-card auto-detection for the client commands.

The kids shouldn't have to type ``--server http://…`` and ``--dest /media/…``
every time. Resolution order for each value:

* ``server``:  explicit flag  →  saved config.
* ``dest``:    explicit flag  →  the single mounted volume that already
  contains a ``.music_cli.db``  →  saved config (e.g. a plain directory used
  for a phone).

After a successful run the resolved values are saved back, so the first
explicit invocation teaches the config and later runs need no flags.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from music_cli.device import DEVICE_DB_NAME

#: Mount roots scanned for cards (each direct child is a candidate volume).
MEDIA_ROOTS = ("/media", "/run/media", "/mnt", "/Volumes")


def config_path() -> Path:
    """Return the config file path, honouring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base else Path.home() / ".config"
    return root / "music-cli" / "config.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    """Persisted client settings."""

    server: str | None = None
    dest: str | None = None


def load(path: Path | None = None) -> Settings:
    """Load saved settings (missing or unreadable file → empty settings)."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text())
    except OSError, tomllib.TOMLDecodeError:
        return Settings()
    server = data.get("server")
    dest = data.get("dest")
    return Settings(
        server=server if isinstance(server, str) else None,
        dest=dest if isinstance(dest, str) else None,
    )


def save(settings: Settings, path: Path | None = None) -> None:
    """Persist settings (only the two known keys; comments not preserved)."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key, value in (("server", settings.server), ("dest", settings.dest)):
        if value is not None:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
    path.write_text("\n".join(lines) + "\n")


def detect_cards(roots: tuple[str, ...] = MEDIA_ROOTS) -> list[Path]:
    """Return mounted volumes that contain a ``.music_cli.db`` at their root.

    Scans one and two levels below each media root (``/media/card`` and
    ``/media/user/card`` layouts).
    """
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if (child / DEVICE_DB_NAME).is_file():
                found.append(child)
                continue
            try:
                grandchildren = sorted(p for p in child.iterdir() if p.is_dir())
            except OSError:
                continue
            found.extend(g for g in grandchildren if (g / DEVICE_DB_NAME).is_file())
    return found


class ResolutionError(Exception):
    """Raised when server or dest cannot be determined."""


def resolve(
    server_flag: str | None,
    dest_flag: str | Path | None,
    *,
    settings: Settings | None = None,
    cards: list[Path] | None = None,
) -> tuple[str, Path]:
    """Resolve the effective ``(server, dest)`` pair.

    Args:
        server_flag: ``--server`` value, if given.
        dest_flag: ``--dest`` value, if given.
        settings: Saved settings (loaded from disk when ``None``).
        cards: Detected card volumes (scanned when ``None``; injectable for
            tests).

    Raises:
        ResolutionError: With a human-friendly message when a value is missing
            or the card is ambiguous.
    """
    settings = settings if settings is not None else load()

    server = server_flag or settings.server
    if not server:
        raise ResolutionError(
            "No server known. Run once with --server http://HOST:8000 "
            "and it will be remembered."
        )

    if dest_flag:
        dest = Path(dest_flag)
    else:
        cards = cards if cards is not None else detect_cards()
        if len(cards) == 1:
            dest = cards[0]
        elif len(cards) > 1:
            listing = ", ".join(str(c) for c in cards)
            raise ResolutionError(
                f"Multiple cards found ({listing}); pick one with --dest."
            )
        elif settings.dest:
            dest = Path(settings.dest)
            if not dest.is_dir():
                # Never silently create a remembered path: a saved card mount
                # that isn't inserted must not become a plain directory.
                raise ResolutionError(
                    f"Remembered destination {dest} does not exist. Insert the "
                    "card, or pass --dest explicitly."
                )
        else:
            raise ResolutionError(
                "No destination known. Insert a previously used card, or run "
                "once with --dest /path/to/card and it will be remembered."
            )
    return server, dest


def remember(server: str, dest: Path, path: Path | None = None) -> None:
    """Save the successfully used server + dest for future runs."""
    save(Settings(server=server, dest=str(dest)), path)
