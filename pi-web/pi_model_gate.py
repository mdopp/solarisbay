#!/usr/bin/env python3
"""`pi-web-model-gate` — the door every model request in this pod goes through (#1435).

The operator's decision of 2026-09-19: picking a model in PI WEB must *activate*
it. The router already loads on demand, so the only thing in the way is the mode
policy — `solaris-llama-policy` answers 409 for a preset the standing mode does
not allow (`templates/llama/post-deploy.py`, `denial`). Taking the mode is the
Engine's `POST /api/model-lease`, and **this pod cannot call it**: it has its own
network namespace (ADR 0007) and that endpoint is loopback-only with no token —
being able to reach it IS the authorisation (`templates/pi-web/variables.json`,
CHAT_PORT; `is_loopback_caller` in the Engine refuses a proxied caller too).

So the two halves are joined over a file, which is the pattern the Engine itself
already uses for exactly the same reason (#1333): a container reaches neither
systemd nor a loopback service, so it writes `gpu_lease_request.json` and a host
`.path` unit runs the broker. Here it is `model-lease/request.json` on the volume
this pod already mounts, and `pi-web-lease-broker.service` on the host drives the
lease API with holder `pi-web`. Nothing of this pod ever addresses port 8787.

What this process is: an OpenAI-compatible front on the pod's own loopback that
every container here talks to instead of talking to `LLAMA_PORT` directly. It
forwards verbatim. When the policy proxy refuses the wanted preset it asks for
the **least intrusive** mode that permits it — `gemma-4-12b` needs `foundry`,
which leaves the voice stack on the GPU; the two Qwen presets need `erweitert`,
which does not; `gemma-4-e4b` needs no mode at all — waits for the broker's
answer and says so in the session while it waits, then forwards the request that
was refused.

The one request it does not forward verbatim is `GET /v1/models`: it answers
that from the router's own list, enriched with the `PRESETS` table below, so the
Pi extension `solaris-llama.js` can fetch a catalog that already carries the
German names, the windows and the thinking switch. Pi's own hourly background
refresh then keeps the model picker current with no script, no timer and no
recurring restart — and because a fetched entry REPLACES a hand-written one of
the same id, that enrichment is what keeps the names from being lost.

Three rules it does not get to bend:

  **It never steals.** A window somebody else holds comes back as 409 naming the
  holder and the end time, not as a takeover. The model tile is a person's
  choice and a person ends it.

  **It gives the card back.** The window is renewed while the session is alive
  and released once it has been idle for `IDLE_RELEASE_S`. If this process dies
  without releasing, the box reclaims the mode on its own after the grace of two
  missed renewals (#1361) — the numbers are in the constants below.

  **It carries no secret.** The request and status files hold a correlation id, a
  mode, a preset name and a deadline. There is no token anywhere on this path,
  because there is no token on the lease API either.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import sys
import threading
import time
import uuid

DEFAULT_GATE_PORT = 11437
DEFAULT_LEASE_DIR = "/data/model-lease"
REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"

# The name every window this pod ever takes is filed under (#1347). One
# permanent name for the *service*, never a session — which is what lets the
# post-deploy recognise and release a leftover window of ours and nobody else's.
HOLDER = "pi-web"

# The window this pod asks for, and the arithmetic that gives the card back when
# this process dies without releasing. `renew_after` is a third of the TTL and
# the box arms its expiry at two missed renewals (#1361,
# `LEASE_GRACE_FACTOR`) — so on a 900 s window the gate renews every 300 s and a
# dead gate loses the mode 600 s after its last renewal, well inside the TTL.
# The idle release is shorter than the grace on purpose: the ordinary way a
# window ends is this process giving it back, not the box taking it.
LEASE_TTL_S = 900
RENEW_AFTER_S = LEASE_TTL_S // 3
IDLE_RELEASE_S = 300
TICK_S = 30

# How long a session waits for a mode switch before it is told to try again.
# The switch was box-measured at ~56 s including the environment change, and a
# cold preset adds 9-19 s on top (#1415).
WAIT_DEADLINE_S = 240
POLL_S = 3
NOTICE_EVERY_S = 15

CHUNK = 64 * 1024
UPSTREAM_TIMEOUT_S = 600

HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Everything this pod knows about a router preset, in ONE table (#1435).
#
# `mode` is the least intrusive window that permits the preset — the three modes
# are `templates/llama/post-deploy.py`'s `LEASE_PROFILES` plus the absence of a
# lease. `foundry` is listed for the 12B rather than `erweitert` because it keeps
# the voice stack and the embeddings server on the GPU, so a 12B session costs
# the household nothing it can notice. An empty string means no window at all.
#
# The rest is what Pi shows and sends: the German display name, the window the
# preset is actually served with, and its thinking switch. It lives here because
# `/v1/models` below answers from it, and a catalog Pi FETCHES replaces the
# entries a person wrote by hand (`@earendil-works/pi-ai/dist/models.js`,
# `createProvider`: same id, fetched wins). Before #1435 those names sat in
# `models.json` on the data volume, written once per deploy — which is how
# `gemma-4-12b` stayed missing from the picker for six days after #1431 started
# serving it. One table, shipped with this image, is the fix.
PRESETS = {
    "gemma-4-e4b": {
        "mode": "",
        "name": "Gemma 4 E4B (Haushaltsmodell)",
        "reasoning": False,
        "input": ["text", "image"],
        "contextWindow": 32768,
        "maxTokens": 16384,
    },
    "gemma-4-12b": {
        "mode": "foundry",
        "name": "Gemma 4 12B (Haushalt + Denken)",
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 131072,
        "maxTokens": 16384,
    },
    "qwen3.6-35b-a3b": {
        "mode": "erweitert",
        "name": "Qwen 3.6 35B-A3B (Denken)",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 131072,
        "maxTokens": 16384,
        "chatTemplateKwargs": {"enable_thinking": True},
    },
    "qwen3.8-27b": {
        "mode": "erweitert",
        "name": "Qwen 3.8 27B (Programmieren)",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 81920,
        "maxTokens": 16384,
        "chatTemplateKwargs": {"enable_thinking": False},
    },
}

# What every model of this provider needs Pi to know about the server, as
# opposed to about the model: llama-server takes neither the `developer` role
# nor `reasoning_effort` — asking for either turns the request into a 400 — and
# `chat-template` is the thinking dialect llama.cpp speaks
# (`chat_template_kwargs.enable_thinking`).
MODEL_COMPAT = {
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "thinkingFormat": "chat-template",
}

# What a preset this table does not know is offered as. It is served by the
# router, so hiding it would be the very staleness this unit removes; it simply
# gets its own id as a name and a window small enough to be safe until somebody
# gives it a row above.
UNKNOWN_CONTEXT = 32768
UNKNOWN_MAX_TOKENS = 16384

MODELS_PATHS = ("/v1/models", "/models")

MODE_LABELS = {
    "foundry": "Foundry",
    "erweitert": "Erweitert",
}


def env(key: str, default: str = "") -> str:
    value = os.environ.get(key, default)
    return value if value else default


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    sys.stdout.write(
        json.dumps({"level": level, "tag": tag, "message": message, "args": args})
        + "\n"
    )
    sys.stdout.flush()


# ── pure decisions (unit-tested in templates/tests) ──────────────────────────


def mode_for_model(model: str) -> str:
    """The window `model` needs, `""` when it needs none or is not ours.

    A preset this table does not know is not a policy question but a typo, and
    the refusal it earned already says so — taking a window for it would switch
    the household's environment for a name nothing serves.
    """
    return str(PRESETS.get(str(model or "").strip(), {}).get("mode") or "")


def pi_model(preset: str) -> dict:
    """What Pi needs to show and call `preset` with.

    A preset with no row in `PRESETS` is still served by the router, so it is
    described rather than hidden: its own id as the name and a small window.
    Hiding it would be exactly the staleness this unit removes.
    """
    row = PRESETS.get(preset, {})
    model = {
        "name": str(row.get("name") or preset),
        "reasoning": bool(row.get("reasoning")),
        "input": list(row.get("input") or ["text"]),
        "contextWindow": int(row.get("contextWindow") or UNKNOWN_CONTEXT),
        "maxTokens": int(row.get("maxTokens") or UNKNOWN_MAX_TOKENS),
        "compat": dict(MODEL_COMPAT),
    }
    kwargs = row.get("chatTemplateKwargs")
    if kwargs:
        model["compat"]["chatTemplateKwargs"] = dict(kwargs)
    return model


def enrich_catalog(body: bytes) -> bytes:
    """The router's `/v1/models` with a `pi` block added to every entry.

    The LIST is the router's, never this table's — so a preset the router has
    stopped serving disappears from Pi's picker instead of lingering until a
    call fails (#1435, the fourth of the operator's conditions). The table only
    ever describes what is there; it cannot add to it.

    Every field the policy proxy sent survives, `allowed_in_mode` and `status`
    included, so a client that reads only `id` is unaffected.
    """
    try:
        listing = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        return body
    listing["data"] = [
        {**entry, "pi": pi_model(entry["id"])}
        for entry in listing["data"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    return json.dumps(listing).encode("utf-8")


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


def requested_model(body: bytes) -> str:
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(request, dict):
        return ""
    model = request.get("model")
    return model.strip() if isinstance(model, str) else ""


def wants_stream(body: bytes) -> bool:
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(request, dict) and request.get("stream") is True


def waiting_notice(mode: str, model: str, waited_s: int) -> str:
    """What the session is told while the box switches the mode.

    Plain German and a number, because the alternative is a minute of nothing
    happening — the operator's third condition on this change.
    """
    if waited_s <= 0:
        return (
            f"Ich hole gerade den Modus {mode_label(mode)}, damit {model} "
            "antworten kann. Das dauert etwa eine Minute.\n"
        )
    return f"… immer noch am Umschalten auf {mode_label(mode)} ({waited_s} s).\n"


def until_text(expires_at: object) -> str:
    if not isinstance(expires_at, (int, float)) or expires_at <= 0:
        return "auf unbestimmte Zeit"
    return "bis " + time.strftime("%H:%M", time.localtime(expires_at)) + " Uhr"


def held_body(model: str, mode: str, record: dict) -> dict:
    """The 409 for a window somebody else holds.

    Same shape and the same job as the policy proxy's own refusal: what was
    refused, who is in the way, and where the remedy is. Never a takeover — the
    holder is usually a person at the model tile.
    """
    holder = str(record.get("holder") or "jemand anderes")
    return {
        "error": {
            "message": (
                f"Modell {model} braucht den Modus {mode_label(mode)}, aber "
                f"{holder} hält die Grafikkarte gerade {until_text(record.get('expires_at'))}. "
                "Die Modell-Kachel in Solaris zeigt, wer hält und bis wann; "
                "solange geht nur ein Modell, das der stehende Modus erlaubt."
            ),
            "mode": mode,
            "holder": holder,
            "expires_at": record.get("expires_at"),
        }
    }


def unavailable_body(model: str, mode: str, detail: str) -> dict:
    return {
        "error": {
            "message": (
                f"Der Modus {mode_label(mode)} für {model} kam nicht zustande. "
                f"{detail} Noch einmal senden hilft oft; sonst zeigt "
                "`journalctl --user -u pi-web-lease-broker` auf der Box, woran es lag."
            ),
            "mode": mode,
        }
    }


def sse_notice(text: str, model: str, now: float) -> bytes:
    """One `chat.completion.chunk` carrying `text` as assistant content.

    A streamed request is the one place a waiting session can be shown anything
    at all, so the wait is spoken in the answer itself rather than hidden in a
    log the resident never opens.
    """
    chunk = {
        "id": "pi-web-model-gate",
        "object": "chat.completion.chunk",
        "created": int(now),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"


# ── the lease, over two files on the shared volume ───────────────────────────


class Lease:
    """This pod's half of the bridge: write a wish, read the answer.

    The host `pi-web-lease-broker.service` is the other half. Nothing here
    implements the lease state machine — that is the whole reason the request
    goes to the broker instead of into a second copy of it.
    """

    def __init__(self, directory: str, clock=time.time, sleep=time.sleep) -> None:
        self.dir = directory
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.mode = ""
        self.last_renew = 0.0
        self.last_seen = clock()
        self.in_flight = 0

    def request_path(self) -> str:
        return os.path.join(self.dir, REQUEST_FILE)

    def status_path(self) -> str:
        return os.path.join(self.dir, STATUS_FILE)

    def write(self, op: str, mode: str = "", model: str = "") -> str:
        """Put the wish on the volume and return its correlation id.

        Written in place rather than renamed into place: the host watcher is a
        `PathChanged=` unit, which sees the close of a write to *this* file and
        would miss a fresh inode moved over it. The record is one short line, so
        the broker — woken after the close — never reads half of it.
        """
        correlation = uuid.uuid4().hex
        record = {
            "id": correlation,
            "op": op,
            "mode": mode,
            "model": model,
            "ttl_s": LEASE_TTL_S,
            "holder": HOLDER,
            "requested_at": self.clock(),
        }
        os.makedirs(self.dir, exist_ok=True)
        with open(self.request_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(record))
        return correlation

    def status(self, correlation: str) -> dict:
        """The broker's answer to `correlation`, `{}` while there is none."""
        try:
            with open(self.status_path(), encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(record, dict) or record.get("id") != correlation:
            return {}
        return record

    def acquire(self, mode: str, on_wait=None) -> dict:
        """Ask for `mode` and wait for the broker. Returns its last answer.

        `retry_after` is read as a *cadence*, not as a duration to sleep once:
        the broker re-states it on every answer while the switch runs.
        """
        correlation = self.write("acquire", mode)
        started = self.clock()
        told = -NOTICE_EVERY_S
        while self.clock() - started < WAIT_DEADLINE_S:
            record = self.status(correlation)
            state = record.get("state")
            if state in ("ready", "held", "error"):
                if state == "ready":
                    with self.lock:
                        self.mode = mode
                        self.last_renew = self.clock()
                return record
            waited = int(self.clock() - started)
            if on_wait is not None and waited - told >= NOTICE_EVERY_S:
                told = waited
                on_wait(waited)
            cadence = record.get("retry_after")
            self.sleep(cadence if isinstance(cadence, (int, float)) else POLL_S)
        return {"state": "timeout"}

    def renew(self) -> None:
        with self.lock:
            mode = self.mode
            self.last_renew = self.clock()
        if mode:
            self.write("acquire", mode)

    def release(self) -> None:
        with self.lock:
            mode = self.mode
            self.mode = ""
        if mode:
            self.write("release", mode)
            jlog("info", "pi-web:gate", "Modus zurückgegeben", mode=mode)

    def touch(self) -> None:
        with self.lock:
            self.last_seen = self.clock()

    def tick(self) -> None:
        """One turn of the keeper: renew while somebody is working, else give
        the card back. Called on a timer, so a long generation with no new
        request of its own still renews."""
        with self.lock:
            if not self.mode:
                return
            busy = self.in_flight > 0 or self.clock() - self.last_seen < IDLE_RELEASE_S
            due = self.clock() - self.last_renew >= RENEW_AFTER_S
        if busy:
            if due:
                self.renew()
            return
        self.release()


def keeper(lease: Lease, tick_s: int = TICK_S) -> None:
    while True:
        time.sleep(tick_s)
        try:
            lease.tick()
        except OSError as e:  # a volume that went away must not kill the door
            jlog("warn", "pi-web:gate", "Lease-Datei nicht schreibbar", error=str(e))


# ── the door ─────────────────────────────────────────────────────────────────


def make_gate_server(
    lease: Lease, listen_port: int, upstream_host: str, upstream_port: int
) -> http.server.ThreadingHTTPServer:
    """The gate, bound and ready to serve. Returned rather than run so the test
    drives the very object the process runs."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        streaming = False

        def log_message(self, fmt: str, *args: object) -> None:
            """A refusal gets a jlog line; a token stream does not get a log."""

        def do_GET(self) -> None:
            if self.path.split("?")[0] in MODELS_PATHS:
                self._catalogue()
                return
            self._relay(self._upstream_call("GET", b""))

        def do_DELETE(self) -> None:
            self._relay(self._upstream_call("DELETE", b""))

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            lease.touch()
            with lease.lock:
                lease.in_flight += 1
            try:
                self._post(body)
            finally:
                with lease.lock:
                    lease.in_flight -= 1

        def _post(self, body: bytes) -> None:
            conn, response = self._upstream_call("POST", body)
            if response is None or response.status != 409:
                self._relay((conn, response))
                return
            model = requested_model(body)
            mode = mode_for_model(model)
            if not mode:
                self._relay((conn, response))
                return
            response.read()
            conn.close()
            self._take_mode(body, model, mode)

        def _take_mode(self, body: bytes, model: str, mode: str) -> None:
            """Ask for the window the refused preset needs, then send the very
            request that was refused."""
            streaming = wants_stream(body)
            if streaming:
                self._open_stream()
                self._say(waiting_notice(mode, model, 0), model)
            jlog(
                "info",
                "pi-web:gate",
                "Modus wird angefordert",
                model=model,
                mode=mode,
                holder=HOLDER,
            )
            record = lease.acquire(
                mode,
                on_wait=(
                    (
                        lambda waited: self._say(
                            waiting_notice(mode, model, waited), model
                        )
                    )
                    if streaming
                    else None
                ),
            )
            state = record.get("state")
            if state == "ready":
                conn, response = self._upstream_call("POST", body)
                if streaming:
                    self._stream_body(conn, response)
                else:
                    self._relay((conn, response))
                return
            if state == "held":
                payload, status = held_body(model, mode, record), 409
            else:
                detail = str(
                    record.get("message") or "Der Umbau lief in eine Zeitgrenze."
                )
                payload, status = unavailable_body(model, mode, detail), 409
            jlog(
                "warn",
                "pi-web:gate",
                payload["error"]["message"],
                model=model,
                mode=mode,
            )
            if streaming:
                self._say(payload["error"]["message"] + "\n", model)
                self._end_stream()
                return
            self._answer(status, json.dumps(payload).encode("utf-8"))

        def _catalogue(self) -> None:
            """`/v1/models`, enriched — the one answer that is not forwarded.

            An upstream that is unreachable or unhappy is passed on as it is
            rather than answered from the table: a catalog invented here would
            put a preset back into Pi's picker that the router no longer serves,
            which is the failure this endpoint exists to prevent.
            """
            conn, response = self._upstream_call("GET", b"")
            if response is None:
                self._answer(502, self._unreachable())
                return
            try:
                status, body = response.status, response.read()
            finally:
                conn.close()
            self._answer(status, enrich_catalog(body) if status == 200 else body)

        # -- transport ----------------------------------------------------

        def _upstream_call(self, method: str, body: bytes):
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS
            }
            if method == "POST":
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(
                upstream_host, upstream_port, timeout=UPSTREAM_TIMEOUT_S
            )
            try:
                conn.request(method, self.path, body=body or None, headers=headers)
                return conn, conn.getresponse()
            except OSError:
                conn.close()
                return None, None

        def _relay(self, pair) -> None:
            conn, response = pair
            if response is None:
                self._answer(502, self._unreachable())
                return
            self._stream_body(conn, response)

        def _stream_body(self, conn, response) -> None:
            if response is None:
                self._answer(502, self._unreachable())
                return
            try:
                # Already answering: the wait notice opened the stream, so the
                # upstream's own header block would be a second one.
                if not self.streaming:
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if (
                            key.lower() not in HOP_HEADERS
                            and key.lower() != "content-length"
                        ):
                            self.send_header(key, value)
                    length = response.getheader("Content-Length")
                    if length is not None:
                        self.send_header("Content-Length", length)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                while True:
                    # read1, not read: `read(n)` on a chunked response keeps
                    # pulling until it has n bytes, which would hold an SSE
                    # stream back until the whole answer is finished.
                    chunk = response.read1(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                self.close_connection = True
            finally:
                conn.close()

        def _open_stream(self) -> None:
            """Start answering before the upstream has been asked, so the wait
            is visible from its first second."""
            self.streaming = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def _say(self, text: str, model: str) -> None:
            try:
                self.wfile.write(sse_notice(text, model, time.time()))
                self.wfile.flush()
            except OSError:
                self.close_connection = True

        def _end_stream(self) -> None:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                self.close_connection = True

        def _answer(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(payload)

        def _unreachable(self) -> bytes:
            return json.dumps(
                {
                    "error": {
                        "message": "Der Modell-Server auf der Box antwortet nicht. "
                        "`journalctl --user -u solaris-llama-policy` sagt warum."
                    }
                }
            ).encode("utf-8")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    lease = Lease(env("PI_MODEL_LEASE_DIR", DEFAULT_LEASE_DIR))
    port = int(env("PI_MODEL_GATE_PORT", str(DEFAULT_GATE_PORT)))
    upstream_port = int(env("LLAMA_PORT", "11435"))
    upstream_host = env("LLAMA_HOST", "host.containers.internal")
    threading.Thread(target=keeper, args=(lease,), daemon=True).start()
    server = make_gate_server(lease, port, upstream_host, upstream_port)
    jlog(
        "info",
        "pi-web:gate",
        "Modell-Tür bereit",
        listen=port,
        upstream=f"{upstream_host}:{upstream_port}",
        lease_dir=lease.dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lease.release()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
