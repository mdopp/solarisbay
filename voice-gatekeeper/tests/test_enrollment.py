"""Tests for the enrolment HTTP endpoints' auth gate.

We don't exercise the embedding/extractor path here — only that the
list endpoint enforces the same Bearer-token gate as enrol/delete
(#437: GET /enrolments must not leak enrolled speaker UIDs to an
unauthenticated caller). The DB path is intentionally absent so
list_uids returns [] without needing a provisioned schema.
"""

from __future__ import annotations

import pytest

from gatekeeper.push import build_combined_app


@pytest.fixture
async def client(aiohttp_client, tmp_path):
    app = build_combined_app(
        piper_uri="tcp://piper:10200",
        devices={},
        push_token="secret",
        db_path=str(tmp_path / "absent.db"),
        speaker_id_enabled=True,
    )
    return await aiohttp_client(app)


async def test_list_enrolments_rejects_missing_token(client):
    resp = await client.get("/enrolments")
    assert resp.status == 401
    body = await resp.json()
    assert body == {"ok": False, "reason": "unauthorized"}


async def test_list_enrolments_rejects_wrong_token(client):
    resp = await client.get("/enrolments", headers={"Authorization": "Bearer nope"})
    assert resp.status == 401


async def test_list_enrolments_allows_correct_token(client):
    resp = await client.get("/enrolments", headers={"Authorization": "Bearer secret"})
    assert resp.status == 200
    body = await resp.json()
    assert body == {"uids": []}
