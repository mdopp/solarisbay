"""ServiceBay companion-read client — the BFF read side (ADR 0010, #811).

Solaris is the BFF/hub: the app talks only to Solaris, never to ServiceBay
directly. ServiceBay already exposes a token-only, proxy-bypassed companion-read
surface for the app (servicebay#2252) — `GET /napi/{home,approvals,services,
upgrades}`. This client consumes those four reads server-to-server so Solaris can
re-serve them under its OWN `/napi/servicebay/*` to the paired device.

Credentials: these reads are `read`-scoped Bearer (servicebay#2252). The client
runs with no acting admin session, so it reads the deploy-time SB-MCP token file
(`sb_mcp_token_path`, read+lifecycle+mutate) — the SAME token the update/approval
pollers use for their unattended polls, NOT a second credential.

Fail-soft: an unreachable ServiceBay, a non-200, or malformed JSON returns None
so the caller can turn it into a 502 for the app rather than crash.

Verdict side (ADR 0010, #811 part 2): the app's [Approve]/[Reject] deep-links to
the Authelia-gated `/api/servicebay/approvals/{id}/{approve,reject}` route, where
Solaris holds the acting admin's TRUSTED forward-auth identity. That verdict runs
under a per-action, single-use, ≤2min `X-SB-Delegated-Admin` assertion minted
from THAT admin's session (servicebay#2276/#2285) — NOT a standing delegation key
in the pod. Solaris mints by forwarding the admin's `authelia_session` cookie to
SB's www portal mint (the delegation analogue of #794's
token-from-authelia-session), where NPM's forward-auth validates it; then
presents the returned assertion PLUS the mutate-scope SB-MCP token to SB's
verdict route, which re-derives the admin against LLDAP before acting.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from solaris_chat.engine.client import current_admin_identity
from solaris_chat.engine.tools.mcp_tools import read_sb_token, read_token
from solaris_chat.logging import log

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# The four companion-read routes ServiceBay exposes (servicebay#2252). Keyed by
# the path segment the app asks Solaris for, so `/napi/servicebay/<key>` maps 1:1.
READ_PATHS = {
    "home": "/napi/home",
    "approvals": "/napi/approvals",
    "services": "/napi/services",
    "upgrades": "/napi/upgrades",
}

# The lifecycle actions SB's operate route accepts (servicebay#2264). Solaris
# rejects anything else before it ever reaches ServiceBay.
OPERATE_ACTIONS = ("start", "stop", "restart")

# SB's session-driven delegated-admin mint (servicebay#2276): a verified Authelia
# admin session (proxy-injected Remote-User/Remote-Groups) mints a short-lived,
# single-use `X-SB-Delegated-Admin` assertion action+target-bound to
# `approvals.approve|deny`. Refuses a client Bearer (no self-elevation) and 401s
# without Remote-User.
_MINT_PATH = "/api/auth/delegated-admin-from-authelia-session"
# The token-scoped verdict routes the assertion is presented to (servicebay#2268):
# the mutate-scope SB-MCP Bearer PLUS the assertion header run the verdict AS the
# named admin. SB uses "deny" (not "reject") on its side.
_VERDICT_PATHS = {"approve": "approve", "reject": "deny"}
# Fallback header name; the mint response echoes `header` (DELEGATION_HEADER).
_DELEGATION_HEADER = "X-SB-Delegated-Admin"


class SbCompanionClient:
    def __init__(
        self,
        sb_api_url: str,
        sb_mcp_token_path: str,
        sb_mint_url: str = "",
        sb_read_token_path: str = "",
    ):
        self._base = sb_api_url.rstrip("/")
        # The delegated-admin mint (servicebay#2276) is the ONLY call that can't
        # use the loopback base: it's a no-Bearer/no-Origin forward-auth POST, so
        # SB's proxy CSRF gate 403s it on :5888 (servicebay#2278). It passes only
        # THROUGH NPM, which injects the CSRF-exempt X-SB-Internal-Token on that
        # route (servicebay#2279). Reads + the verdict carry a Bearer, so they
        # satisfy the CSRF gate over loopback and stay on `_base`. Empty mint
        # base ⇒ fall back to `_base` (the pre-#2279 loopback path).
        self._mint_base = (sb_mint_url or sb_api_url).rstrip("/")
        self._token_path = sb_mcp_token_path
        self._read_token_path = sb_read_token_path

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    async def read(self, key: str) -> dict[str, Any] | None:
        """Fetch one companion read (`home`/`approvals`/`services`/`upgrades`).

        Returns ServiceBay's JSON body verbatim (the app renders it directly), or
        None when SB is unreachable / non-200 / malformed / unknown key.

        Reads the non-expiring read-only SB token (servicebay#2302,
        `sb_read_token_path`) so it never 401-churns when the deploy-time SB-MCP
        token rotates, falling back to that token when the read-token file is
        absent. The mutating `operate`/`submit_verdict` paths keep the SB-MCP
        token — a read-only token would be refused there."""
        path = READ_PATHS.get(key)
        if path is None or not self._base:
            return None
        token = read_sb_token(self._read_token_path, self._token_path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self._base}{path}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warn(
                            "engine.sb_companion.http", key=key, status=resp.status
                        )
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError, OSError) as e:
            log.warn("engine.sb_companion.fetch_failed", key=key, error=str(e))
            return None

    async def operate(self, name: str, action: str) -> tuple[bool, str]:
        """Run a lifecycle action (`start`/`stop`/`restart`) on a ServiceBay
        service for the app (BFF, ADR 0010, #827 operate half).

        POSTs to SB's lifecycle-scoped `POST /napi/services/:name/operate`
        (servicebay#2264) with the deploy-time SB-MCP token — the SAME
        read+lifecycle+mutate token the reads use, no new credential. The
        lifecycle scope is what authorises this on SB's side; a `read`-only token
        would be refused there, so least privilege lives in the token's scope.

        Rejects an action outside `OPERATE_ACTIONS` before any network call.
        Returns `(ok, detail)` — `ok` iff SB returned 2xx; a bad action, no
        token, unreachable SB, or non-2xx returns `(False, <reason>)`, never a
        false ok."""
        if action not in OPERATE_ACTIONS:
            return False, "bad_action"
        if not self._base:
            return False, "no_sb_api"
        token = read_token(self._token_path)
        if not token:
            return False, "no_token"
        url = f"{self._base}/napi/services/{name}/operate"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.post(
                    url, headers=headers, json={"action": action}
                ) as resp:
                    text = (await resp.text())[:400]
                    if resp.status // 100 != 2:
                        log.warn(
                            "engine.sb_companion.operate_http",
                            name=name,
                            action=action,
                            status=resp.status,
                        )
                        return False, f"HTTP {resp.status}: {text}"
                    return True, text or "ok"
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warn(
                "engine.sb_companion.operate_failed",
                name=name,
                action=action,
                error=str(e),
            )
            return False, str(e)

    async def submit_verdict(
        self, approval_id: str, verb: str, authelia_cookie: str = ""
    ) -> tuple[bool, str]:
        """Deliver the acting admin's verdict on an approval to ServiceBay
        (ADR 0010, #811 part 2). `verb` is `approve` or `reject`.

        Two server-to-server hops, both fail-closed on the report side:
          1. mint a per-action, single-use `X-SB-Delegated-Admin` assertion from
             the acting admin's LIVE Authelia session (servicebay#2276/#2285) —
             Solaris forwards that admin's `authelia_session` cookie to the www
             portal mint, where NPM's forward-auth validates it and injects the
             CSRF-exempt X-SB-Internal-Token;
          2. present that assertion PLUS the mutate-scope SB-MCP Bearer to SB's
             verdict route, which re-derives the admin against LLDAP and acts.

        NO standing delegation key is held here; the assertion is ephemeral and
        bound to THIS admin + action + approval id. Returns `(ok, detail)` —
        `ok` iff SB returned 2xx; any failure (no admin identity, no cookie, mint
        refusal, non-2xx, unreachable SB) returns `(False, <reason>)`, never a
        false ok."""
        if verb not in _VERDICT_PATHS or not self._base:
            return False, "bad_verb" if verb not in _VERDICT_PATHS else "no_sb_api"
        user, groups = current_admin_identity.get()
        if not user:
            return False, "no_admin_identity"
        if not authelia_cookie:
            return False, "no_authelia_cookie"
        action = "approvals.approve" if verb == "approve" else "approvals.deny"
        assertion, header = await self._mint_delegation(
            action, approval_id, authelia_cookie
        )
        if not assertion:
            return False, "mint_failed"
        token = read_token(self._token_path)
        if not token:
            return False, "no_token"
        sb_verb = _VERDICT_PATHS[verb]
        url = f"{self._base}/napi/approvals/{approval_id}/{sb_verb}"
        headers = {"Authorization": f"Bearer {token}", header: assertion}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.post(url, headers=headers) as resp:
                    text = (await resp.text())[:400]
                    if resp.status // 100 != 2:
                        log.warn(
                            "engine.sb_companion.verdict_http",
                            verb=verb,
                            status=resp.status,
                        )
                        return False, f"HTTP {resp.status}: {text}"
                    return True, text or "ok"
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warn("engine.sb_companion.verdict_failed", verb=verb, error=str(e))
            return False, str(e)

    async def _mint_delegation(
        self, action: str, target: str, authelia_cookie: str
    ) -> tuple[str, str]:
        """Mint the single-use `X-SB-Delegated-Admin` assertion
        (servicebay#2276/#2285) from the acting admin's live Authelia session.
        Forwards the admin's `authelia_session` cookie to the www portal mint —
        NPM's forward-auth validates it, derives Remote-User/Remote-Groups, and
        injects the CSRF-exempt X-SB-Internal-Token (NO client Bearer — the mint
        403s a token caller). Names the exact action+target the assertion may be
        used for. Returns `(assertion, header_name)`, or `("", "")` on failure."""
        url = f"{self._mint_base}{_MINT_PATH}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.post(
                    url,
                    headers={"Cookie": f"authelia_session={authelia_cookie}"},
                    json={"action": action, "target": target},
                ) as resp:
                    if resp.status != 200:
                        log.warn("engine.sb_companion.mint_http", status=resp.status)
                        return "", ""
                    body = await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError, OSError) as e:
            log.warn("engine.sb_companion.mint_failed", error=str(e))
            return "", ""
        if not isinstance(body, dict):
            return "", ""
        assertion = body.get("assertion")
        header = body.get("header") or _DELEGATION_HEADER
        if not isinstance(assertion, str) or not assertion:
            return "", ""
        return assertion, header if isinstance(
            header, str
        ) and header else _DELEGATION_HEADER
