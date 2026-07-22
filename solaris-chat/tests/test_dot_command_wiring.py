"""Behaviour-wiring guards for the `.tool` dot-commands (ADR 0009).

The other frontend tests assert that a symbol *exists*. These assert that each
dot-command is actually WIRED to do its job — the create posts the right action,
and every result row is actionable (opens / edits / toggles), not an inert label.
They exist because "looks wired but the click does nothing" bugs (a note result
with no click handler, duplicate hits, a create pointed at the wrong action) slip
past string-presence checks. Still static (regex over the served HTML), so they
gate the wiring, not the rendered runtime — the real proof is the box-verify.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _has(pattern: str) -> bool:
    return re.search(pattern, _HTML) is not None


def test_all_dot_commands_registered_and_dispatched():
    # Every `.tool` is offered, has a head label, and dispatches to a builder.
    for cmd in ("task", "note", "doc", "contacts", "home", "energy"):
        assert _has(r'\["\.' + cmd + r'",'), f".{cmd} missing from DOT_COMMANDS"
        assert _has(r'(?:else )?if \(cmd === "' + cmd + r'"\) build'), (
            f".{cmd} not dispatched in ensureCard"
        )


def test_task_create_find_edit_wired():
    # create → task.add; a row is a checkbox that toggles task.set_status; tapping
    # the row opens the inline editor which PATCHes task.update.
    assert _has(r'taskAction\(\s*"task\.add"')
    assert _has(r'taskAction\(\s*"task\.set_status"')
    assert "beginTaskEdit(el, row, t)" in _HTML
    assert _has(r'taskAction\(\s*"task\.update"')


def test_note_create_and_clickable_deduped_results():
    # create → note.add; results are de-duplicated by display LABEL (an upload's
    # companion + its extracted OKF note share a title) and each row is CLICKABLE,
    # opening the note viewer — not an inert label.
    assert _has(r'taskAction\(\s*"note\.add"')
    assert "byLabel[key]" in _HTML  # de-dupe by display label, not path
    assert "openNoteViewer(u.path)" in _HTML
    # kept restrictive: every query word must appear in the hit.
    assert "words.every" in _HTML


def test_contacts_create_find_edit_wired():
    # create → contact.add; tapping a contact row opens the editor → person.update.
    assert _has(r'taskAction\(\s*"contact\.add"')
    assert "beginPersonEdit(el, rw, c)" in _HTML
    assert _has(r'taskAction\(\s*"person\.update"')


def test_doc_upload_and_search_wired():
    # upload classifies (doc.classify); typing filters via the search endpoint.
    assert _has(r'taskAction\(\s*"doc\.classify"')
    assert "function searchDocs(el, q)" in _HTML
    assert '"/api/portal/documents/search' in _HTML


def test_home_filters_devices_and_reflects_favourite():
    # typing filters devices live; matches render as controllable widget cards
    # that carry the favourite ★ toggle (pin:true).
    assert _has(r'cmd === "home"[\s\S]*?renderHomeList\(card')
    assert "renderHaCard(c, false, { pin: true })" in _HTML


def test_energy_renders_inline():
    # .energy is its own display-only card that reuses the energy renderer.
    assert "function buildEnergyCard(el)" in _HTML
    assert "renderEnergyPage(listEl, j.energy)" in _HTML


def test_favourite_toggle_adds_and_removes():
    # the ★/☆ on a card both pins (POST) and unpins (DELETE by id) — a real
    # toggle that reflects hcPins state, not a one-way pin.
    assert "hcPins" in _HTML
    assert '"/api/favorites"' in _HTML
    assert _has(r'"/api/favorites/"\s*\+')  # DELETE by favourite id
