"""Consumer side of the Pact contract.

Drives the real :class:`~music_cli.client.MusicClient` against a Pact mock
server and writes the pact file to ``tests/contract/pacts/`` for the provider
verification test to check. Runs before ``test_provider_verify`` by alphabetical
collection order.
"""

import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from pact import Pact
from pact import match

from music_cli.client import MusicClient

pytestmark = pytest.mark.contract

PACTS_DIR = Path(__file__).parent / "pacts"
CONSUMER = "music-cli-client"
PROVIDER = "music-cli-server"

# The exact track the provider fixture will expose (see conftest).
TRACK_PATH = "Queen/Greatest Hits/03 Bohemian Rhapsody.flac"
TRACK_BYTES = b"audio-bytes"


async def test_consumer_contract_and_write_pact():
    pact = Pact(CONSUMER, PROVIDER).with_specification("V4")

    (
        pact.upon_receiving("a catalog metadata request")
        .with_request("GET", "/catalog/meta")
        .will_respond_with(200)
        .with_body(
            {
                "schema_version": match.string("1"),
                "generated_at": match.string("2026-07-05T00:00:00+00:00"),
                "track_count": match.integer(3),
                "etag": match.string('"7000-abc"'),
                "size": match.integer(28672),
            },
            content_type="application/json",
        )
    )

    (
        pact.upon_receiving("an audio file download")
        .with_request("GET", f"/files/{quote(TRACK_PATH)}")
        .will_respond_with(200)
        .with_body(TRACK_BYTES.decode(), content_type="audio/flac")
    )

    with pact.serve() as srv:
        async with httpx.AsyncClient() as http:
            client = MusicClient(str(srv.url), http)

            meta = await client.get_meta()
            assert meta["schema_version"] == "1"
            assert isinstance(meta["track_count"], int)

            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "track.flac"
                written = await client.download_track(TRACK_PATH, target)
                assert written == len(TRACK_BYTES)
                assert target.read_bytes() == TRACK_BYTES

    PACTS_DIR.mkdir(parents=True, exist_ok=True)
    pact.write_file(PACTS_DIR, overwrite=True)
    assert (PACTS_DIR / f"{CONSUMER}-{PROVIDER}.json").exists()
