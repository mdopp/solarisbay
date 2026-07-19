"""Injected on-disk locations for the vendored Google-Takeout importers.

The standalone `solaris-import-google` tool read every data path from a
module-level `app.config` that resolved environment variables at import time.
In-engine there is no such global — the engine already knows the Radicale and
file-share trees — so the paths are passed in explicitly as this small frozen
value instead. Nothing here reads the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImporterPaths:
    """Data trees the importers read from / write into.

    - `radicale_data` — Radicale multifilesystem root (holds `collections/`).
    - `music_dir` / `audiobooks_dir` — scanned for library ownership; audiobooks
      may be `None` when the tree doesn't exist.
    - `data_dir` — writable scratch dir (the ytmusicapi album cache lands here).
    """

    radicale_data: Path
    music_dir: Path
    data_dir: Path
    audiobooks_dir: Path | None = None
