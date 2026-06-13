"""Resident-registration tool — the onboarding flow's enrol-then-file step.

The guest-onboarding dialog (#375) hands off here when an unknown speaker
chooses to register: this tool takes the candidate's chosen uid + display name
and the captured voice samples, enrols the voice through the gatekeeper's
`voice_enrol` accessor (#364), and — only on a successful enrolment — files a
`pending_residents` row (#376) for the admin-approval step (#355) to act on.

Biometric care, same as `enrol.py`: the raw `samples` are a biometric
identifier, so they never appear in the result or any log line — only the uid,
display name and the gatekeeper's ok/reason surface. Enrolment failure is passed
through verbatim and **no** pending row is written, so the dialog never claims a
registration that didn't enrol. This is an onboarding-only tool, not part of the
household or general guest toolset — see profiles.py for where it joins.
"""

from __future__ import annotations

import json
from typing import Any

from solilos_chat import pending_residents_store
from solilos_chat.engine.tools import Tool
from solilos_chat.engine.tools.enrol import build_enrol_tools


def build_register_tools(
    db_path: str, gatekeeper_url: str, gatekeeper_token: str = ""
) -> list[Tool]:
    (enrol_tool,) = build_enrol_tools(gatekeeper_url, gatekeeper_token)

    async def register(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        display_name = str(args.get("display_name") or "").strip()
        if not display_name:
            return json.dumps({"ok": False, "reason": "missing_display_name"})

        # Enrol first; the enrol tool validates uid/samples and passes the
        # gatekeeper's verdict through. Only a true success files a request.
        enrol_result = json.loads(
            await enrol_tool.handler({"uid": uid, "samples": args.get("samples")})
        )
        if not enrol_result.get("ok"):
            return json.dumps(
                {"ok": False, "reason": enrol_result.get("reason", "enrol_failed")},
                ensure_ascii=False,
            )

        request_id = pending_residents_store.add_pending_resident(
            db_path, uid=uid, display_name=display_name, enrolled=True
        )
        return json.dumps(
            {"ok": True, "uid": uid, "request_id": request_id, "status": "pending"},
            ensure_ascii=False,
        )

    return [
        Tool(
            name="register_pending_resident",
            description=(
                "Schließt die Bewohner-Registrierung ab: enrollt die Stimme beim"
                " Gatekeeper und legt eine Freigabe-Anfrage an (Onboarding). uid +"
                " display_name + base64-PCM-samples. Kein Konto bis zur Freigabe."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "display_name": {"type": "string"},
                    "samples": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "base64 16kHz mono int16 PCM samples",
                    },
                },
                "required": ["uid", "display_name", "samples"],
            },
            handler=register,
        ),
    ]
