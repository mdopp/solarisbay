"""Sync provider organizations to the phone book as vCards (#doc-graph).

Every document's provider is an `organization` entity carrying contact facts
(phone/email/address/contact_person) — see `ingest/obsidian.py`. This projects
each org into a shared "Solaris Anbieter" address book as one vCard, so the
providers show up as contacts on the household's phones.

**Authenticated CardDAV PUT**, not a filesystem mount: the sync runs as a
dedicated `solaris` DAV account (its own LLDAP identity), and Radicale's
`owner_only` rights scope that account to only its own collection — no raw
storage access, no other resident's data, no loosened perms. The vCard UID is
derived from the entity id, so a re-sync overwrites the same card instead of
duplicating, and never touches a human's own contacts (different UIDs). Disabled
(no-op) when the collection URL / credentials are unset.
"""

from __future__ import annotations

import vobject

from solaris_chat.engine.ingest.dav_client import HttpDavClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.logging import log

# A vCard UID that marks the card as Solaris-owned, so a re-sync overwrites the
# same resource and a human's own contacts (different UIDs) are never touched.
_UID_PREFIX = "solaris-provider-"
_CONTACT_PREDICATES = ("phone", "email", "address", "contact_person")


def _org_contact(conn, entity_id: str) -> dict[str, str]:
    """The org's contact facts, highest-confidence value winning per predicate."""
    out: dict[str, str] = {}
    for f in conn.execute(
        "SELECT predicate, value FROM facts WHERE subject_entity_id = ?"
        " ORDER BY confidence DESC",
        (entity_id,),
    ).fetchall():
        if f["predicate"] in _CONTACT_PREDICATES and f["predicate"] not in out:
            out[f["predicate"]] = f["value"]
    return out


def _build_vcard(entity_id: str, name: str, contact: dict[str, str]) -> str:
    card = vobject.vCard()
    card.add("fn").value = name
    card.add("n").value = vobject.vcard.Name(family=name)
    card.add("org").value = [name]
    if contact.get("phone"):
        card.add("tel").value = contact["phone"]
    if contact.get("email"):
        card.add("email").value = contact["email"]
    if contact.get("address"):
        card.add("adr").value = vobject.vcard.Address(street=contact["address"])
    note = "Anbieter aus deinen Solaris-Dokumenten."
    if contact.get("contact_person"):
        note = f"Ansprechpartner: {contact['contact_person']}. {note}"
    card.add("note").value = note
    card.add("uid").value = _UID_PREFIX + entity_id
    return card.serialize()


async def sync_contacts(
    db_path: str, collection_url: str, username: str, password: str
) -> dict[str, int]:
    """PUT every provider org's vCard into the Solaris address book collection.

    Returns `{written, failed}`. Never raises — a sync failure must not abort the
    night run. No-op when the collection URL or credentials are unset."""
    if not (collection_url and username and password):
        return {"written": 0, "failed": 0}
    client = HttpDavClient(carddav_username=username, carddav_password=password)
    written = failed = 0
    conn = projection.open_conn(db_path)
    try:
        orgs = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE type = 'organization'"
            " ORDER BY canonical_name"
        ).fetchall()
        rows = [
            (o["id"], o["canonical_name"], _org_contact(conn, o["id"])) for o in orgs
        ]
    finally:
        conn.close()
    for entity_id, name, contact in rows:
        try:
            await client.put_item(
                collection_url,
                _UID_PREFIX + entity_id,
                _build_vcard(entity_id, name, contact),
                suffix=".vcf",
                content_type="text/vcard; charset=utf-8",
            )
            written += 1
        except Exception as e:  # noqa: BLE001 — one bad card must not stop the sync.
            log.error("engine.contacts_sync.card_failed", org=name, error=str(e))
            failed += 1
    log.info("engine.contacts_sync", written=written, failed=failed)
    return {"written": written, "failed": failed}
