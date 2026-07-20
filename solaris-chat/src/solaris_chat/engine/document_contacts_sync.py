"""Sync provider organizations to the phone book as vCards (#doc-graph).

Every document's provider is an `organization` entity carrying contact facts
(phone/email/address/contact_person) — see `ingest/obsidian.py`. This projects
each org into the owner's Radicale address book as one vCard, so the providers
show up among the household's real contacts on their phones.

Filesystem write (like `solaris-import-google`): Radicale runs `owner_only`, so
there is no admin DAV path — we drop `<uid>.vcf` files straight into the storage
tree under the org owner's `contacts/` collection and let Radicale rescan. The
vCard UID is derived from the entity id, so a re-sync overwrites in place instead
of duplicating. Disabled (no-op) when `radicale_data` is unset.
"""

from __future__ import annotations

from pathlib import Path

import vobject

from solaris_chat import notes_search
from solaris_chat.engine.importers.google_takeout import radicale_store
from solaris_chat.engine.knowledge import projection
from solaris_chat.logging import log

# The owner's personal address book — the same collection the Google-import
# writes to, so providers land among the resident's existing contacts.
_COLLECTION = "contacts"
# A vCard UID that marks the card as Solaris-owned, so a re-sync overwrites the
# same file and a human's own contacts (different UIDs) are never touched.
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


def sync_contacts(db_path: str, radicale_data: str) -> dict[str, int]:
    """Write every provider org's vCard into its owner's Radicale address book.

    Returns `{written, skipped}`. Household-scoped orgs have no personal book
    (no LDAP principal) and are skipped. Never raises — a sync failure must not
    abort the night run."""
    if not radicale_data:
        return {"written": 0, "skipped": 0}
    data = Path(radicale_data)
    written = skipped = 0
    conn = projection.open_conn(db_path)
    try:
        orgs = conn.execute(
            "SELECT id, canonical_name, resident_uid FROM entities"
            " WHERE type = 'organization' ORDER BY resident_uid, canonical_name"
        ).fetchall()
        with radicale_store.storage_lock(data):
            for org in orgs:
                user = org["resident_uid"]
                # The shared/household scope maps to no single Radicale principal;
                # only per-resident orgs sync to a real address book.
                if not user or user == notes_search.SHARED_UID:
                    skipped += 1
                    continue
                coll = radicale_store.ensure_collection(
                    data, user, _COLLECTION, tag="VADDRESSBOOK", displayname="Kontakte"
                )
                vcard = _build_vcard(
                    org["id"], org["canonical_name"], _org_contact(conn, org["id"])
                )
                radicale_store.write_item(coll, _UID_PREFIX + org["id"], vcard, "vcf")
                written += 1
    except Exception as e:  # noqa: BLE001 — sync must never crash the night run.
        log.error("engine.contacts_sync.failed", error=str(e))
    finally:
        conn.close()
    log.info("engine.contacts_sync", written=written, skipped=skipped)
    return {"written": written, "skipped": skipped}
