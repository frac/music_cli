"""Provider side of the Pact contract.

Starts the real FastAPI provider as a subprocess and verifies it against the
pact file produced by ``test_consumer_pact`` using ``pact.Verifier``.
"""

import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from pact import Verifier

from tests.integration.test_catalog_build import make_library

pytestmark = pytest.mark.contract

PACT_FILE = Path(__file__).parent / "pacts" / "music-cli-client-music-cli-server.json"


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
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"provider at {url} did not become healthy")


@pytest.fixture
def provider_url(tmp_path: Path) -> Iterator[str]:
    library = tmp_path / "library"
    library.mkdir()
    make_library(library)  # includes the exact track the pact references
    db = tmp_path / "catalog.db"
    port = _free_port()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "music_cli",
            "catalog",
            "build",
            "--root",
            str(library),
            "--db",
            str(db),
        ],
        check=True,
    )
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


def test_provider_honours_pact(provider_url: str):
    assert PACT_FILE.exists(), "run test_consumer_pact first to generate the pact"
    verifier = Verifier("music-cli-server", "127.0.0.1")
    verifier.add_transport(url=provider_url)
    verifier.add_source(PACT_FILE)
    verifier.verify()  # raises on any interaction mismatch
