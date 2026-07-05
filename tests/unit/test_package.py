"""Smoke test confirming the package imports and exposes a version."""

import music_cli


def test_package_imports():
    assert music_cli.__version__
