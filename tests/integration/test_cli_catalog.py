"""Integration test for the `music catalog` CLI subcommand."""

from pathlib import Path

from music_cli.cli import main
from tests.integration.test_catalog_build import make_library


def test_cli_catalog_build_and_update(tmp_path: Path, capsys):
    make_library(tmp_path)
    db = tmp_path / "catalog.db"

    assert main(["catalog", "build", "--root", str(tmp_path), "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "3 tracks" in out
    assert db.exists()

    (tmp_path / "Queen/Greatest Hits/05 Somebody.flac").write_bytes(b"x")
    rc = main(["catalog", "update", "--root", str(tmp_path), "--db", str(db)])
    assert rc == 0
    assert "+1" in capsys.readouterr().out
