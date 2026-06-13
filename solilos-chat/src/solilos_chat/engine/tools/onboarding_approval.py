"""Resident-onboarding approval + provisioning — the admin side of #355.

The guest onboarding flow (#376) enrols an unknown speaker's voice and files a
`pending_residents` row. This module is the admin-gated step that surfaces that
request to the human and finishes the provisioning once they approve:

  file_resident_approval(uid)  → files the pending request onto ServiceBay's
      central access-request list (the one place the admin already approves
      Authelia/SB access in, #343). Passing the uid as the LLDAP `username`
      lets the admin one-click Approve to auto-provision the SSO account — so
      *SB* owns the Authelia account; Solilos only owns the resident record and
      the voice-profile binding. The returned request id is stored on the row.

  check_resident_approval(uid) → polls that request id via
      get_access_request_status. While "pending" nothing changes. On "resolved"
      Solilos provisions its side: it marks the pending row approved and
      confirms the enrolled voice profile (voice_embeddings, written by the
      gatekeeper under this same uid during onboarding) is present and bound to
      the uid. The admin is the gate — Solilos never approves itself.

Open question deferred to #355 refinement: get_access_request_status returns
only "resolved", with no approved-vs-denied signal, so the deny path (drop the
captured biometrics via the gatekeeper's DELETE /enrolments/{uid}) is not yet
implementable from the tool contract. This module treats "resolved" as approved
and does not delete; the deny/cleanup half waits on that contract decision.

Biometric care, as elsewhere: no embedding bytes ever cross this module — only
the uid, display name and a present/absent verdict.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from solilos_chat import pending_residents_store
from solilos_chat.engine.tools import Tool
from solilos_chat.engine.tools.mcp_tools import call_sb_tool


def _voice_profile_bound(db_path: str, uid: str) -> bool:
    """True if a voice_embeddings row exists for the uid (the onboarding
    enrolment, #386). The table is keyed by uid, so a present row *is* the
    binding — there is nothing to re-key."""
    if not Path(db_path).exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM voice_embeddings WHERE uid = ?", (uid,)
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def build_onboarding_approval_tools(
    db_path: str, sb_mcp_url: str, sb_mcp_token_path: str
) -> list[Tool]:
    async def file_approval(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        pending = pending_residents_store.get_pending_by_uid(db_path, uid)
        if pending is None:
            return json.dumps({"ok": False, "reason": "no_pending_request"})
        if pending.get("request_id"):
            return json.dumps(
                {"ok": True, "request_id": pending["request_id"], "status": "filed"}
            )

        filed = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "file_access_request",
                {
                    "subject": pending["display_name"],
                    "kind": "resident",
                    "username": uid,
                    "payload": "Solilos resident onboarding — voice profile enrolled.",
                    "requested_by": "solilos-onboarding",
                },
            )
        )
        request_id = filed.get("id") or filed.get("request_id")
        if not request_id:
            return json.dumps({"ok": False, "reason": "file_failed", "detail": filed})
        pending_residents_store.set_request_id(db_path, pending["id"], str(request_id))
        return json.dumps(
            {"ok": True, "request_id": str(request_id), "status": "filed"}
        )

    async def check_approval(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        pending = pending_residents_store.get_pending_by_uid(db_path, uid)
        if pending is None:
            return json.dumps({"ok": False, "reason": "no_pending_request"})
        request_id = pending.get("request_id")
        if not request_id:
            return json.dumps({"ok": False, "reason": "not_filed"})

        polled = json.loads(
            await call_sb_tool(
                sb_mcp_url,
                sb_mcp_token_path,
                "get_access_request_status",
                {"id": str(request_id)},
            )
        )
        status = polled.get("status")
        if status != "resolved":
            # pending / not-found → no provisioning, surface the raw status.
            return json.dumps({"ok": True, "status": status, "provisioned": False})

        pending_residents_store.mark_approved(db_path, pending["id"])
        return json.dumps(
            {
                "ok": True,
                "status": "approved",
                "provisioned": True,
                "uid": uid,
                "voice_profile_bound": _voice_profile_bound(db_path, uid),
            }
        )

    return [
        Tool(
            name="file_resident_approval",
            description=(
                "Reicht eine ausstehende Bewohner-Registrierung zur Freigabe in"
                " der zentralen ServiceBay-Anfrageliste ein (Onboarding,"
                " admin-only). uid der Kandidat:in. Gibt die Anfrage-id zurück;"
                " das Konto entsteht erst, wenn der Admin dort freigibt."
            ),
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=file_approval,
        ),
        Tool(
            name="check_resident_approval",
            description=(
                "Prüft den Freigabe-Status einer eingereichten"
                " Bewohner-Registrierung (admin-only). Bei Freigabe schließt es"
                " die Solilos-Seite ab: markiert die Anfrage als freigegeben und"
                " bestätigt das gebundene Sprachprofil. Solilos gibt nie selbst"
                " frei."
            ),
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=check_approval,
        ),
    ]
