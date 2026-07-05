"""Integration tests for the FastAPI server (in-process, via httpx ASGI)."""

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from music_cli import catalog
from music_cli.server import create_app
from tests.integration.test_catalog_build import make_library

TRACK = "Pink Floyd/The Wall/01 - In the Flesh.mp3"


def client_for(root: Path, db: Path) -> httpx.AsyncClient:
    app = create_app(root, db)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _prepared(tmp_path: Path) -> tuple[Path, Path]:
    make_library(tmp_path)
    db = tmp_path / "catalog.db"
    await catalog.build(tmp_path, db)
    return tmp_path, db


async def test_health(tmp_path: Path):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_catalog_meta(tmp_path: Path):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get("/catalog/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == str(catalog.SCHEMA_VERSION)
    assert body["track_count"] == 3
    assert body["etag"]


async def test_meta_503_when_no_catalog(tmp_path: Path):
    make_library(tmp_path)
    async with client_for(tmp_path, tmp_path / "missing.db") as c:
        resp = await c.get("/catalog/meta")
    assert resp.status_code == 503


async def test_catalog_db_download_and_304(tmp_path: Path):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get("/catalog.db")
        assert resp.status_code == 200
        assert resp.content[:16].startswith(b"SQLite format 3")
        etag = resp.headers["etag"]

        again = await c.get("/catalog.db", headers={"If-None-Match": etag})
        assert again.status_code == 304


async def test_files_download(tmp_path: Path):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get(f"/files/{quote(TRACK)}")
    assert resp.status_code == 200
    assert resp.content == b"audio-bytes"


async def test_files_range_request(tmp_path: Path):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get(f"/files/{quote(TRACK)}", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == b"audi"


@pytest.mark.parametrize(
    "target",
    ["/files/does-not-exist.mp3", "/files/../server.py", "/files/..%2f..%2fetc"],
)
async def test_files_missing_or_traversal(tmp_path: Path, target: str):
    root, db = await _prepared(tmp_path)
    async with client_for(root, db) as c:
        resp = await c.get(target)
    assert resp.status_code in (403, 404)
