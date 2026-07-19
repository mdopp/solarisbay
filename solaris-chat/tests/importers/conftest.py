"""Fixtures for the vendored Google-Takeout importer tests.

The standalone tool read data paths from a module-level env config; in-engine
they are injected as an ``ImporterPaths`` value, so each test gets a throwaway
tree via the ``paths`` fixture instead of environment variables.
"""

import pytest

from solaris_chat.engine.importers.google_takeout import library
from solaris_chat.engine.importers.google_takeout.paths import ImporterPaths


@pytest.fixture()
def paths(tmp_path) -> ImporterPaths:
    """A throwaway data tree (radicale + music + scratch) for one test."""
    radicale = tmp_path / "radicale" / "data"
    music = tmp_path / "music"
    audiobooks = tmp_path / "audiobooks"
    data = tmp_path / "data"
    (radicale / "collections").mkdir(parents=True, exist_ok=True)
    music.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    library._cache.update({"sig": None, "keys": set(), "by_artist": {}, "count": 0})
    return ImporterPaths(
        radicale_data=radicale,
        music_dir=music,
        data_dir=data,
        audiobooks_dir=audiobooks,
    )


@pytest.fixture()
def music_dir(paths):
    """An isolated, empty music library, with the scan cache reset."""
    d = paths.music_dir

    def add(artist: str, album: str, title: str):
        p = d / artist / album
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{title}.mp3").write_bytes(b"")  # untagged → path fallback

    return type("Lib", (), {"dir": d, "add": staticmethod(add)})
