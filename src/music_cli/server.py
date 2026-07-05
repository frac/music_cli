"""A deliberately thin FastAPI server.

Because the client browses and diffs a *downloaded* copy of the catalog, the
server needs no dynamic browse endpoints. It only:

* reports catalog freshness (``/catalog/meta``) so clients can skip re-downloads,
* serves the SQLite snapshot (``/catalog.db``) with ``ETag`` / ``304`` support,
* serves the audio tree (``/files/...``) via Starlette ``StaticFiles`` (bundled
  with FastAPI), which provides HTTP range requests, correct MIME types and
  path-traversal safety.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi import Header
from fastapi import HTTPException
from fastapi import Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from music_cli.catalog import read_meta


def file_etag(path: Path) -> str:
    """Return a strong-ish ETag derived from a file's size and mtime."""
    stat = path.stat()
    return f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'


def create_app(root: str | Path, db_path: str | Path) -> FastAPI:
    """Build the FastAPI application.

    Args:
        root: Library root directory whose audio files are served under ``/files``.
        db_path: Path to the SQLite catalog snapshot.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    root = Path(root)
    db_path = Path(db_path)
    app = FastAPI(title="Music CLI server")

    def require_db() -> Path:
        if not db_path.exists():
            raise HTTPException(status_code=503, detail="catalog not built yet")
        return db_path

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/catalog/meta")
    async def catalog_meta() -> dict[str, object]:
        path = require_db()
        meta = await read_meta(path)
        return {
            "schema_version": meta.get("schema_version"),
            "generated_at": meta.get("generated_at"),
            "track_count": int(meta.get("track_count", 0)),
            "etag": file_etag(path),
            "size": path.stat().st_size,
        }

    @app.get("/catalog.db")
    async def catalog_db(if_none_match: str | None = Header(default=None)):
        path = require_db()
        etag = file_etag(path)
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return FileResponse(
            path,
            media_type="application/vnd.sqlite3",
            filename="catalog.db",
            headers={"ETag": etag},
        )

    app.mount("/files", StaticFiles(directory=root), name="files")
    return app
