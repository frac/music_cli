"""End-to-end test: run the real server and client as subprocesses.

Self-contained — spawns ``python -m music_cli serve`` and drives it with
``python -m music_cli sync``; no docker-compose required.
"""

import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:  # URLError and ConnectionError both subclass OSError
            time.sleep(0.2)
    raise RuntimeError(f"server at {url} did not become healthy")


def _music(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "music_cli", *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    for rel, data in {
        "Queen/Hits/03 Bohemian Rhapsody.flac": b"bohemian",
        "Pink Floyd/The Wall/01 - In the Flesh.mp3": b"in-the-flesh",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def server(library: Path, tmp_path: Path) -> Iterator[str]:
    db = tmp_path / "catalog.db"
    _music("catalog", "build", "--root", str(library), "--db", str(db))
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "music_cli",
            "serve",
            "--root",
            str(library),
            "--db",
            str(db),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(url)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_serve_then_sync_copies_selected_artist(server: str, tmp_path: Path):
    card = tmp_path / "card"

    result = _music(
        "sync", "--server", server, "--dest", str(card), "--artist", "Queen"
    )
    assert "Copied 1" in result.stdout

    copied = card / "Queen/Hits/03 Bohemian Rhapsody.flac"
    assert copied.read_bytes() == b"bohemian"
    # The other artist was not selected.
    assert not (card / "Pink Floyd").exists()

    # Re-running is a no-op.
    again = _music("sync", "--server", server, "--dest", str(card), "--artist", "Queen")
    assert "Copied 0" in again.stdout
