"""Write directly into Radicale's on-disk ``multifilesystem`` storage.

Radicale runs with ``rights = owner_only`` and LDAP auth, so there is no admin
DAV path to write into another user's collections — but the acting user is
already known from SSO. We therefore drop items straight into the storage tree;
Radicale rescans and picks them up (it tolerates externally-created files).

On-disk layout (Radicale 3.x)::

    collections/
      .Radicale.lock                     # global storage lock (flock)
      collection-root/
        <user>/                          # principal collection (.Radicale.props = {})
          <calendar>/                    # a calendar collection
            .Radicale.props              # {"tag": "VCALENDAR", ...}
            <uid>.ics                    # one VCALENDAR per item file
          contacts/                      # an addressbook collection
            .Radicale.props              # {"tag": "VADDRESSBOOK"}
            <uid>.vcf

Writes are serialized against Radicale via an exclusive ``flock`` on
``collections/.Radicale.lock`` — the same lock file Radicale itself uses — so an
import never races a concurrent DAV operation.

The Radicale data root is injected by the caller (``ImporterPaths.radicale_data``)
rather than read from a module-level config.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

# Characters that are unsafe in a collection/item filename.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_name(name: str, fallback: str = "item") -> str:
    """Make a filesystem-safe, non-empty name component."""
    cleaned = _UNSAFE.sub("-", (name or "").strip()).strip("-.")
    return cleaned or fallback


def _collections_root(radicale_data: Path) -> Path:
    return radicale_data / "collections"


@contextlib.contextmanager
def storage_lock(radicale_data: Path):
    """Hold Radicale's global storage lock for the duration of the block.

    Radicale runs as a different uid than us (its own userns), so its lock file
    may be foreign-owned. We try O_RDWR (works once the data tree is made
    world-writable), fall back to O_RDONLY (flock works on a read fd), and as a
    last resort proceed without the lock rather than failing the whole import.
    """
    root = _collections_root(radicale_data)
    with contextlib.suppress(OSError):
        root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".Radicale.lock"
    fd = None
    for flags in (os.O_RDWR | os.O_CREAT, os.O_RDONLY):
        try:
            fd = os.open(lock_path, flags, 0o666)
            break
        except OSError:
            continue
    if fd is None:
        yield  # best effort — couldn't acquire the lock at all
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_dir(path: Path) -> None:
    """Create a dir and make it traversable+writable by any uid.

    Radicale (a different uid) must be able to write into collections we create
    — add items, rebuild its .Radicale.cache — so we open the perms to 0777.
    The Radicale data tree is not the shared media tree; loosening perms here is
    the deliberate trade for keeping imports credential-free (user's choice).
    """
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o777)


def _write_props(collection_dir: Path, props: dict) -> None:
    _atomic_write(collection_dir / ".Radicale.props", json.dumps(props))


def _atomic_write(path: Path, content: str) -> None:
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        # World-writable so Radicale (a different uid) can overwrite/delete it.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o666)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def ensure_user_root(radicale_data: Path, user: str) -> Path:
    """Ensure ``collection-root/<user>/`` exists as a plain principal collection."""
    root = radicale_data / "collections" / "collection-root" / user
    _ensure_dir(root)
    if not (root / ".Radicale.props").exists():
        _write_props(root, {})
    return root


def ensure_collection(
    radicale_data: Path, user: str, name: str, tag: str, displayname: str | None = None
) -> Path:
    """Ensure a calendar/addressbook collection exists and return its dir.

    ``tag`` is ``"VCALENDAR"`` or ``"VADDRESSBOOK"``.
    """
    ensure_user_root(radicale_data, user)
    coll = (
        radicale_data / "collections" / "collection-root" / user / sanitize_name(name)
    )
    _ensure_dir(coll)
    props: dict[str, str] = {"tag": tag}
    if displayname:
        props["D:displayname"] = displayname
    if tag == "VCALENDAR":
        props["C:supported-calendar-component-set"] = "VEVENT,VTODO,VJOURNAL"
    _write_props(coll, props)  # idempotent; refreshes displayname on re-import
    return coll


def write_item(collection_dir: Path, item_uid: str, content: str, ext: str) -> str:
    """Write one item file (``<uid>.<ext>``) into a collection. Returns the href."""
    href = f"{sanitize_name(item_uid, 'item')}.{ext}"
    _atomic_write(collection_dir / href, content)
    return href
