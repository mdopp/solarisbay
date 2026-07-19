"""Import Google Contacts ``.vcf`` exports into the acting user's Radicale
address book (CardDAV collection ``<user>/contacts/``).

Google exports one ``.vcf`` (``Takeout/Contacts/*.vcf``, vCard 3.0) containing
many ``VCARD``s. CardDAV storage wants one file per card, so we split them. Each
card gets a stable ``UID`` (kept from the source when present, otherwise derived
from the card content) so re-imports overwrite instead of duplicating.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import vobject

from .. import radicale_store

_NS = uuid.UUID("6f1a1c2e-9b1e-4b7a-9c2d-000000000002")
_CONTACTS_COLLECTION = "contacts"


def _iter_cards(vcf_text: str):
    for card in vobject.readComponents(vcf_text):
        yield card


def _ensure_uid(card) -> str:
    uid = None
    if hasattr(card, "uid") and card.uid.value:
        uid = str(card.uid.value)
    if not uid:
        fn = card.fn.value if hasattr(card, "fn") else ""
        # Serialize to hash the whole card for a stable synthetic UID.
        uid = f"import-{uuid.uuid5(_NS, fn + '|' + card.serialize())}"
        card.add("uid").value = uid
    return uid


def _decode(vcf_bytes: bytes) -> str:
    return vcf_bytes.decode("utf-8", errors="replace")


def _names(vcf_text: str, limit=5):
    out = []
    for i, card in enumerate(_iter_cards(vcf_text)):
        if i >= limit:
            break
        out.append(str(card.fn.value) if hasattr(card, "fn") else "(ohne Namen)")
    return out


def _count(vcf_text: str) -> int:
    return sum(1 for _ in _iter_cards(vcf_text))


def preview(filename: str, vcf_bytes: bytes) -> dict:
    text = _decode(vcf_bytes)
    return {
        "type": "contacts",
        "cards": _count(text),
        "samples": _names(text),
    }


def do_import(radicale_data: Path, user: str, filename: str, vcf_bytes: bytes) -> dict:
    text = _decode(vcf_bytes)
    written = 0
    with radicale_store.storage_lock(radicale_data):
        coll = radicale_store.ensure_collection(
            radicale_data,
            user,
            _CONTACTS_COLLECTION,
            tag="VADDRESSBOOK",
            displayname="Kontakte",
        )
        for card in _iter_cards(text):
            uid = _ensure_uid(card)
            radicale_store.write_item(coll, uid, card.serialize(), "vcf")
            written += 1
    return {
        "type": "contacts",
        "written": written,
        "target": f"{user}/{_CONTACTS_COLLECTION}",
    }
