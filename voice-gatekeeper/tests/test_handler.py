"""Tests for client-id derivation from the socket peer.

Regression guard for the previously-undefined `self.client_id` referenced
in the conversation endpoint: Wyoming exposes no client identity, so it is
derived from the connection peer here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from wyoming.info import Describe

from gatekeeper.handler import GatekeeperHandler, client_id_from_peername


class _StubInfo:
    """Minimal stand-in for wyoming.info.Info — only `.event()` is used."""

    def event(self):
        return "info-event"


def test_client_id_from_tcp_peername():
    assert client_id_from_peername(("192.168.178.42", 53124)) == "192.168.178.42"


def test_client_id_from_unix_socket_path():
    assert client_id_from_peername("/run/wyoming/sat.sock") == "/run/wyoming/sat.sock"


def test_client_id_none_when_peer_missing():
    assert client_id_from_peername(None) is None
    assert client_id_from_peername(()) is None


def test_client_id_none_when_host_empty():
    assert client_id_from_peername(("", 5000)) is None


def test_handler_constructs_with_info_arg():
    # Regression: the server builds GatekeeperHandler(r, w, _info());
    # forwarding the 3rd arg to Wyoming's base used to raise TypeError.
    handler = GatekeeperHandler(None, None, _StubInfo())
    assert handler.client_id is None  # no writer -> peer unavailable
    assert isinstance(handler._info, _StubInfo)


async def test_handler_answers_describe():
    handler = GatekeeperHandler(None, None, _StubInfo())
    handler.write_event = AsyncMock()
    handled = await handler.handle_event(Describe().event())
    assert handled is True
    handler.write_event.assert_awaited_once_with("info-event")
