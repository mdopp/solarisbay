"""The mode policy proxy in front of the router (#1416).

The router enforces nothing: asked for a preset, it loads it, and with
`--models-max 1` that evicts whatever the household was answering from. So the
router moved to loopback and this proxy holds `LLAMA_PORT`, reading the lease's
`allowed` set per request.

Three things here are load-bearing and none shows up as a failure when it breaks:
a proxy that forgets to refuse is exactly the bug the operator asked to have
closed, a proxy that *buffers* turns every streamed answer into a long silence
followed by the whole text at once, and a listing that drops what the mode
forbids hides the other three presets from anyone whose only door is this port
(#1431). All are exercised here against a fake upstream rather than mocked,
because the thing under test is HTTP behaviour, not a function's return value.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("llama_pd_policy", TEMPLATES / "llama" / "post-deploy.py")


# Shaped like the router's own answer, `status.value` included: that field is
# the load state, a different axis from the mode's permission, and it has to
# survive the proxy untouched.
CATALOGUE = {
    "object": "list",
    "data": [
        {"id": "gemma-4-e4b", "object": "model", "status": {"value": "unloaded"}},
        {"id": "gemma-4-12b", "object": "model", "status": {"value": "unloaded"}},
        {"id": "qwen3.6-35b-a3b", "object": "model", "status": {"value": "unloaded"}},
        {"id": "qwen3.8-27b", "object": "model", "status": {"value": "loaded"}},
    ],
}

PRESETS = [entry["id"] for entry in CATALOGUE["data"]]


@pytest.fixture
def upstream(pd):
    """A fake router. `gate` lets a test hold the second SSE frame back until
    the client has really received the first one."""
    import http.server

    state = {"gate": None, "seen": [], "streamed": None}

    class Router(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _json(self, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["seen"].append(("GET", self.path))
            if self.path.startswith("/v1/models"):
                self._json(CATALOGUE)
            elif self.path == "/health":
                self._json({"status": "ok"})
            elif self.path == "/slots":
                self._json([{"speculative": True}])
            else:
                self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            request = json.loads(body or b"{}")
            state["seen"].append(("POST", self.path, request.get("model")))
            if not request.get("stream"):
                self._json({"model": request.get("model"), "echo": True})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b'data: {"n": 1}\n\n')
            if state["gate"] is not None:
                # The proxy is only streaming if the client already has frame
                # one while frame two has not been written yet. A buffering
                # proxy leaves this waiting until it times out, which is what
                # the test asserts on.
                state["streamed"] = state["gate"].wait(timeout=5)
            self._chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _chunk(self, payload: bytes):
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

    import http.server as hs

    server = hs.ThreadingHTTPServer(("127.0.0.1", 0), Router)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.state = state
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy(pd, tmp_path, upstream):
    server = pd.make_proxy_server(str(tmp_path), 0, upstream.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _lease(pd, tmp_path, mode: str, allowed: list[str]) -> None:
    path = pathlib.Path(pd.lease_file(str(tmp_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"holder": "test", "mode": mode, "allowed": allowed, "ready": True})
    )


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, json.loads(response.read())


# ── what the policy reads ──────────────────────────────────────────────────


def test_no_lease_allows_only_the_household_preset(pd, tmp_path):
    assert pd.proxy_policy(str(tmp_path)) == (["gemma-4-e4b"], "household")


def test_the_household_default_follows_the_deployed_alias(pd, tmp_path, monkeypatch):
    """An operator who deployed other weights is asked for those — the same
    `llama-profile.json` a release reloads."""
    monkeypatch.setenv("LLAMA_MODEL_ALIAS", "gemma-4-e4b-de")
    pd.save_household_profile(str(tmp_path))
    monkeypatch.delenv("LLAMA_MODEL_ALIAS")
    assert pd.proxy_policy(str(tmp_path)) == (["gemma-4-e4b-de"], "household")


def test_a_mode_allows_what_the_lease_wrote(pd, tmp_path):
    _lease(pd, tmp_path, "foundry", ["gemma-4-e4b", "gemma-4-12b"])
    assert pd.proxy_policy(str(tmp_path)) == (
        ["gemma-4-e4b", "gemma-4-12b"],
        "foundry",
    )


def test_an_exclusive_lease_allows_nothing(pd, tmp_path):
    _lease(pd, tmp_path, "exclusive", [])
    assert pd.proxy_policy(str(tmp_path)) == ([], "exclusive")


def test_the_requested_model_survives_every_shape_a_client_sends(pd):
    assert pd.requested_model(b'{"model": " qwen3.8-27b "}') == "qwen3.8-27b"
    assert pd.requested_model(b'{"messages": []}') == ""
    assert pd.requested_model(b'{"model": null}') == ""
    assert pd.requested_model(b"not json") == ""
    assert pd.requested_model(b"[]") == ""
    assert pd.requested_model(b"") == ""


def test_the_denial_says_the_mode_the_list_and_what_to_do(pd):
    body = pd.denial("qwen3.8-27b", "household", ["gemma-4-e4b"])
    assert body["error"]["mode"] == "household"
    assert body["error"]["allowed"] == ["gemma-4-e4b"]
    message = body["error"]["message"]
    # pi_autoloop greps `"mode":` out of this body and names the tile in its
    # ticket protocol — both halves have to stay findable.
    assert "qwen3.8-27b" in message and "household" in message
    assert "gemma-4-e4b" in message and "Modell-Kachel" in message


def test_an_exclusive_lease_says_the_card_is_gone_rather_than_listing_nothing(pd):
    message = pd.denial("gemma-4-e4b", "exclusive", [])["error"]["message"]
    assert "exklusiv" in message


# ── what the proxy does on the wire ────────────────────────────────────────


def test_a_preset_the_mode_allows_is_forwarded(pd, tmp_path, proxy, upstream):
    _lease(pd, tmp_path, "coding", ["qwen3.8-27b"])
    status, body = _post(f"{proxy}/v1/chat/completions", {"model": "qwen3.8-27b"})
    assert status == 200
    assert body == {"model": "qwen3.8-27b", "echo": True}
    assert ("POST", "/v1/chat/completions", "qwen3.8-27b") in upstream.state["seen"]


def test_a_preset_outside_the_mode_never_reaches_the_router(
    pd, tmp_path, proxy, upstream
):
    """The whole point: served, this request would evict the household model
    and cost the next resident turn a 10-20 s reload."""
    status, body = _post(f"{proxy}/v1/chat/completions", {"model": "qwen3.8-27b"})
    assert status == 409
    assert body["error"]["mode"] == "household"
    assert body["error"]["allowed"] == ["gemma-4-e4b"]
    assert upstream.state["seen"] == []


@pytest.mark.parametrize(
    "path", ["/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/completion"]
)
def test_every_model_carrying_endpoint_is_policed_not_just_chat(
    pd, tmp_path, proxy, upstream, path
):
    _lease(pd, tmp_path, "thinking", ["qwen3.6-35b-a3b"])
    status, _ = _post(f"{proxy}{path}", {"model": "gemma-4-12b"})
    assert status == 409
    assert upstream.state["seen"] == []


def test_a_request_naming_no_model_is_forwarded(pd, tmp_path, proxy, upstream):
    """The router answers it from the preset it already has resident, which
    cannot be one outside the mode — there is nothing here to refuse."""
    _lease(pd, tmp_path, "coding", ["qwen3.8-27b"])
    status, _ = _post(f"{proxy}/v1/chat/completions", {"messages": []})
    assert status == 200
    assert ("POST", "/v1/chat/completions", None) in upstream.state["seen"]


@pytest.mark.parametrize(
    "mode,allowed",
    [
        ("household", ["gemma-4-e4b"]),
        ("foundry", ["gemma-4-e4b", "gemma-4-12b"]),
        ("coding", ["qwen3.8-27b"]),
        ("exclusive", []),
    ],
)
def test_the_model_list_names_every_preset_in_every_mode(
    pd, tmp_path, proxy, mode, allowed
):
    """#1431: filtered to the mode, this list hid the other three presets from
    a client whose only door is this port — it could not learn they exist."""
    _lease(pd, tmp_path, mode, allowed)
    status, body = _get(f"{proxy}/v1/models")
    assert status == 200
    assert [entry["id"] for entry in body["data"]] == PRESETS


@pytest.mark.parametrize(
    "mode,allowed",
    [
        ("foundry", ["gemma-4-e4b", "gemma-4-12b"]),
        ("coding", ["qwen3.8-27b"]),
        ("exclusive", []),
    ],
)
def test_the_model_list_marks_which_presets_the_mode_allows(
    pd, tmp_path, proxy, mode, allowed
):
    _lease(pd, tmp_path, mode, allowed)
    _, body = _get(f"{proxy}/v1/models")
    assert body["mode"] == mode
    assert {entry["id"]: entry["allowed_in_mode"] for entry in body["data"]} == {
        preset: preset in allowed for preset in PRESETS
    }


def test_the_model_list_without_a_lease_marks_the_household_preset(pd, tmp_path, proxy):
    _, body = _get(f"{proxy}/v1/models")
    assert body["mode"] == "household"
    assert [entry["id"] for entry in body["data"] if entry["allowed_in_mode"]] == [
        "gemma-4-e4b"
    ]


def test_the_load_state_is_passed_through_and_is_not_the_permission(
    pd, tmp_path, proxy
):
    """Two axes: `status.value` says where the weights are, `allowed_in_mode`
    says whether this client may ask. An allowed preset is routinely unloaded
    — normal, and 7-17 s on the first turn."""
    _lease(pd, tmp_path, "foundry", ["gemma-4-e4b", "gemma-4-12b"])
    _, body = _get(f"{proxy}/v1/models")
    entries = {entry["id"]: entry for entry in body["data"]}
    assert entries["gemma-4-12b"]["status"]["value"] == "unloaded"
    assert entries["gemma-4-12b"]["allowed_in_mode"] is True
    assert entries["qwen3.8-27b"]["status"]["value"] == "loaded"
    assert entries["qwen3.8-27b"]["allowed_in_mode"] is False


def test_a_client_that_reads_only_the_id_still_parses_the_listing(pd, tmp_path, proxy):
    """The marking is additive: an OpenAI client knows `object` and `data[].id`
    and nothing else, and must not trip over what was added beside them."""
    _lease(pd, tmp_path, "coding", ["qwen3.8-27b"])
    _, body = _get(f"{proxy}/v1/models")
    assert body["object"] == "list"
    for entry in body["data"]:
        assert entry["object"] == "model"
    assert "qwen3.8-27b" in [entry["id"] for entry in body["data"]]


def test_the_listing_is_not_the_gate(pd, tmp_path, proxy, upstream):
    """Listed and marked not-allowed is still refused at the request — the
    409 path is unchanged by #1431."""
    _lease(pd, tmp_path, "coding", ["qwen3.8-27b"])
    _, body = _get(f"{proxy}/v1/models")
    listed = {entry["id"]: entry["allowed_in_mode"] for entry in body["data"]}
    assert listed["gemma-4-12b"] is False
    status, refusal = _post(f"{proxy}/v1/chat/completions", {"model": "gemma-4-12b"})
    assert status == 409
    assert refusal["error"]["mode"] == "coding"
    assert refusal["error"]["allowed"] == ["qwen3.8-27b"]
    assert ("POST", "/v1/chat/completions", "gemma-4-12b") not in upstream.state["seen"]


def test_health_and_slots_pass_through(pd, tmp_path, proxy, upstream):
    assert _get(f"{proxy}/health") == (200, {"status": "ok"})
    assert _get(f"{proxy}/slots") == (200, [{"speculative": True}])
    assert ("GET", "/health") in upstream.state["seen"]


def test_a_stream_is_passed_through_frame_by_frame(pd, tmp_path, proxy, upstream):
    """`http.client.read()` fills its buffer before it returns, so a proxy
    built on it holds the whole answer back until generation is finished. The
    upstream here refuses to write the second frame until the client has the
    first, which a buffering proxy can never satisfy."""
    upstream.state["gate"] = threading.Event()
    _lease(pd, tmp_path, "coding", ["qwen3.8-27b"])
    request = urllib.request.Request(
        f"{proxy}/v1/chat/completions",
        data=json.dumps({"model": "qwen3.8-27b", "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        first = response.read(len(b'data: {"n": 1}\n\n'))
        assert first == b'data: {"n": 1}\n\n'
        upstream.state["gate"].set()
        assert response.read() == b"data: [DONE]\n\n"
    # True only if the first frame reached this client before the upstream
    # produced the second one — i.e. nothing in between held the stream.
    assert upstream.state["streamed"] is True


def test_a_dead_router_is_a_502_with_the_mode_rather_than_a_dropped_socket(
    pd, tmp_path, upstream
):
    """During an exclusive lease `llama.service` is stopped but the proxy stays
    up — a resident's client should get a sentence, not a refused connection."""
    dead = upstream.server_address[1]
    upstream.shutdown()
    upstream.server_close()
    server = pd.make_proxy_server(str(tmp_path), 0, dead)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _post(f"{url}/v1/chat/completions", {"model": "gemma-4-e4b"})
        assert status == 502
        assert body["error"]["mode"] == "household"
    finally:
        server.shutdown()
        server.server_close()


# ── how it gets onto the box ───────────────────────────────────────────────


def test_the_unit_runs_the_proxy_verb_of_the_installed_script(pd):
    unit = pd.render_policy_unit(
        "/mnt/data/stacks", "11435", "11434", "/x/gpu-lease.py"
    )
    assert "/x/gpu-lease.py proxy" in unit
    assert "Environment=DATA_DIR=/mnt/data/stacks" in unit
    assert "Environment=LLAMA_PORT=11435" in unit
    assert "Environment=LLAMA_ROUTER_PORT=11434" in unit
    # Nothing else listens on LLAMA_PORT, so a crash must not be permanent.
    assert "Restart=always" in unit


def test_installing_the_proxy_enables_and_restarts_it(pd, tmp_path, monkeypatch):
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path))
    )
    pd.install_policy_unit(str(tmp_path), "11435", "11434", "/x/gpu-lease.py")
    unit = tmp_path / ".config" / "systemd" / "user" / f"{pd.POLICY_UNIT}.service"
    assert unit.exists()
    # The restart is not decoration: the script the unit runs was rewritten by
    # this same install, and the old process would police the old table.
    assert calls == [
        ("enable", ("--now", f"{pd.POLICY_UNIT}.service")),
        ("restart", (f"{pd.POLICY_UNIT}.service",)),
    ]


def test_the_router_is_unreachable_from_off_the_box(pd):
    """The proxy is only a policy if it is the only door: a router on 0.0.0.0
    would be reachable from every sibling pod and the mode would mean nothing
    again."""
    args = pd.server_args("11434", "/models")
    assert args[:4] == ["--host", "127.0.0.1", "--port", "11434"]
    template = (TEMPLATES / "llama" / "template.yml").read_text(encoding="utf-8")
    assert '- "--host"\n    - "127.0.0.1"' in template
    assert '- "{{LLAMA_ROUTER_PORT}}"' in template
    assert '- "0.0.0.0"' not in template


def test_the_router_port_is_declared_with_a_default(pd):
    variables = json.loads(
        (TEMPLATES / "llama" / "variables.json").read_text(encoding="utf-8")
    )
    assert variables["LLAMA_ROUTER_PORT"]["default"] == "11434"
    # LLAMA_PORT is the port on the outside and carries the LAN decision (#1420);
    # the router port carries no flag because it never leaves loopback.
    assert variables["LLAMA_PORT"]["blockLanAccess"] is False
    assert "blockLanAccess" not in variables["LLAMA_ROUTER_PORT"]
