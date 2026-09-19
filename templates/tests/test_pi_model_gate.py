"""The bridge that lets a PI WEB session take the mode its model needs (#1435).

The pod cannot call the Engine's lease API: `/api/model-lease` is loopback-only
and carries no token — being able to reach it IS the authorisation — and this pod
has its own network namespace (ADR 0007). So a wish goes onto the shared volume
and a demand-driven host unit makes the call. Everything asserted here has a
wrong answer that looks like a working install:

* **The mode per model.** Asking for `erweitert` where `foundry` would do moves
  the household's voice stack onto the CPU and switches off vault search for the
  whole window — for a model that needed neither. Nobody would see a bug; the
  house would just get quietly worse.
* **The wait.** A mode switch is ~56 s. A session that shows nothing in that
  minute is indistinguishable from one that has hung, and the operator's own
  condition on this change was that it must not look like that.
* **The return.** A window nobody gives back is the failure #1392 was about. The
  gate releases on idle, and if it dies the box reclaims after the grace —
  the two numbers have to agree with `templates/llama/post-deploy.py`.
* **Somebody else's window.** Stealing it would take the card away from a person
  who chose it at the model tile.
* **The netns boundary.** A single `127.0.0.1:8787` anywhere in the pod is a call
  that cannot work, and it would look like a transport error rather than like a
  design mistake.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys

import pytest
import yaml

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
PI_WEB = TEMPLATES / "pi-web"
ROOT = TEMPLATES.parent
GATE = ROOT / "pi-web" / "pi_model_gate.py"


def _code(path: pathlib.Path) -> str:
    """The file with its prose removed.

    Docstrings and comments have to name the endpoint this pod may not call —
    that is how the next reader learns why the bridge exists. Only what the
    machine executes is asserted on.
    """
    if path.suffix == ".js":
        return re.sub(r"//[^\n]*|/\*.*?\*/", "", path.read_text("utf-8"), flags=re.S)
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef)
        ) and ast.get_docstring(node):
            node.body[0] = ast.Pass()
    return ast.unparse(tree)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load("pi_model_gate", GATE)


@pytest.fixture(scope="module")
def pd():
    return _load("pi_web_pd_gate", PI_WEB / "post-deploy.py")


@pytest.fixture(scope="module")
def pod() -> dict:
    text = (PI_WEB / "template.yml").read_text(encoding="utf-8")
    variables = json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))
    for name, spec in variables.items():
        text = text.replace("{{%s}}" % name, str(spec.get("default", "x")))
    text = text.replace("{{PUBLIC_DOMAIN}}", "example.test")
    text = text.replace("{{DATA_DIR}}", "/mnt/data/stacks")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def llama():
    return _load("llama_pd_gate", TEMPLATES / "llama" / "post-deploy.py")


class Clock:
    """A clock the test winds itself, so a 900 s window costs no wall time."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def lease(gate, tmp_path):
    clock = Clock()
    return gate.Lease(str(tmp_path / "model-lease"), clock=clock, sleep=clock.sleep)


def answer(lease_obj, **fields) -> None:
    """Write the broker's answer to whatever id is pending."""
    request = json.loads(pathlib.Path(lease_obj.request_path()).read_text("utf-8"))
    pathlib.Path(lease_obj.status_path()).write_text(
        json.dumps({"id": request["id"], **fields}), "utf-8"
    )


# ── the mode a model needs ───────────────────────────────────────────────────


def test_the_least_intrusive_mode_that_permits_the_model_is_chosen(gate):
    """`foundry` keeps the voice stack and the embeddings server on the GPU;
    `erweitert` takes both away. A 12B session must not cost the house what a
    MoE session costs it."""
    assert gate.mode_for_model("gemma-4-12b") == "foundry"
    assert gate.mode_for_model("qwen3.6-35b-a3b") == "erweitert"
    assert gate.mode_for_model("qwen3.8-27b") == "erweitert"


def test_the_household_model_needs_no_window_at_all(gate):
    """`haushalt` IS the absence of a lease and it allows e4b. Taking a window
    for it would switch the environment for nothing."""
    assert gate.mode_for_model("gemma-4-e4b") == ""


def test_a_preset_nothing_serves_is_a_typo_and_not_a_policy_question(gate):
    """An unknown name earns the router's own refusal; switching the
    household's environment for it would be worse than the error."""
    assert gate.mode_for_model("gpt-9") == ""
    assert gate.mode_for_model("") == ""


def test_every_mode_the_gate_asks_for_is_one_the_box_knows(gate, llama):
    """A name the box does not know is a 400 the session would see as a hang."""
    known = set(llama.LEASE_PROFILES) | set(llama.LEASE_MODE_ALIASES)
    for row in gate.PRESETS.values():
        if row["mode"]:
            assert row["mode"] in known, row["mode"]


def test_every_preset_the_box_serves_has_a_mode_here(gate, llama):
    """A preset added to the router without a row here would be refused for
    ever: the gate would read it as a typo and never ask for its mode."""
    for preset in llama.preset_profiles():
        assert preset in gate.PRESETS, preset


# ── the catalog Pi fetches (#1435) ───────────────────────────────────────────


def catalog(gate, *ids: str) -> dict:
    """What the policy proxy answers `/v1/models` with, for `ids`."""
    return json.loads(
        gate.enrich_catalog(
            json.dumps(
                {
                    "object": "list",
                    "mode": "haushalt",
                    "data": [
                        {"id": name, "object": "model", "allowed_in_mode": False}
                        for name in ids
                    ],
                }
            ).encode("utf-8")
        ).decode("utf-8")
    )


def test_the_catalog_carries_the_names_and_limits_pi_would_otherwise_lose(gate):
    """Once a fetch exists, pi-ai merges by id and the FETCHED entry replaces
    the hand-written one (`createProvider` in `dist/models.js`). So everything a
    person would have written into models.json has to travel in this answer, or
    the refresh that keeps the list fresh is what throws the German names,
    the windows and the thinking switch away."""
    entries = {
        entry["id"]: entry["pi"] for entry in catalog(gate, *gate.PRESETS)["data"]
    }
    assert entries["qwen3.8-27b"]["name"] == "Qwen 3.8 27B (Programmieren)"
    assert entries["qwen3.6-35b-a3b"]["name"] == "Qwen 3.6 35B-A3B (Denken)"
    assert entries["gemma-4-e4b"]["name"] == "Gemma 4 E4B (Haushaltsmodell)"
    assert entries["gemma-4-12b"]["name"] == "Gemma 4 12B (Haushalt + Denken)"
    assert entries["qwen3.8-27b"]["contextWindow"] == 81920
    assert entries["qwen3.8-27b"]["maxTokens"] == 16384
    assert entries["qwen3.8-27b"]["compat"]["chatTemplateKwargs"] == {
        "enable_thinking": False
    }
    assert entries["qwen3.6-35b-a3b"]["compat"]["chatTemplateKwargs"] == {
        "enable_thinking": True
    }
    # Gemma has no thinking mode; declaring one would send kwargs its template
    # does not know.
    assert "chatTemplateKwargs" not in entries["gemma-4-e4b"]["compat"]
    assert entries["gemma-4-e4b"]["reasoning"] is False
    assert entries["qwen3.8-27b"]["reasoning"] is True


def test_every_entry_says_what_llama_server_cannot_take(gate):
    """`developer` and `reasoning_effort` turn a call into a 400, and
    `chat-template` is the thinking dialect llama.cpp speaks. The extension
    registers this provider on its own, so the compat has to ride on the model
    rather than on the models.json entry."""
    for entry in catalog(gate, *gate.PRESETS)["data"]:
        compat = entry["pi"]["compat"]
        assert compat["supportsDeveloperRole"] is False
        assert compat["supportsReasoningEffort"] is False
        assert compat["thinkingFormat"] == "chat-template"


def test_the_windows_match_the_presets_the_router_is_configured_with(gate, llama):
    """The window is ours, not the router's `n_ctx_train`: a preset is served
    with the `ctx-size` its profile names, and promising Pi more than that is a
    request the server cannot honour."""
    profiles = llama.preset_profiles()
    for preset, row in gate.PRESETS.items():
        assert row["contextWindow"] == int(profiles[preset]["context_length"]), preset


def test_a_preset_the_router_stopped_serving_disappears(gate):
    """The fourth of the operator's conditions: a vanished preset must not
    linger until a call fails. The LIST is always the router's — this table
    only describes what is in it — so dropping a preset from `presets.ini` is
    enough to take it out of Pi's picker."""
    listed = [entry["id"] for entry in catalog(gate, "gemma-4-e4b")["data"]]
    assert listed == ["gemma-4-e4b"]


def test_a_preset_with_no_row_here_is_described_rather_than_hidden(gate):
    """The opposite direction: a preset added to the router but not yet to the
    table is exactly the case that broke `gemma-4-12b`. It is offered under its
    own id with a safe window until somebody names it."""
    entry = catalog(gate, "gemma-5-2b")["data"][0]
    assert entry["pi"]["name"] == "gemma-5-2b"
    assert entry["pi"]["contextWindow"] == gate.UNKNOWN_CONTEXT
    assert entry["pi"]["reasoning"] is False


def test_the_proxys_own_fields_survive_the_enrichment(gate):
    """`allowed_in_mode` and the standing mode are #1431's; a client reading
    only `id` must be unaffected by this endpoint existing."""
    listing = catalog(gate, "gemma-4-e4b")
    assert listing["mode"] == "haushalt"
    assert listing["data"][0]["allowed_in_mode"] is False
    assert listing["data"][0]["object"] == "model"


def test_an_answer_that_is_not_a_catalog_is_passed_through_untouched(gate):
    """An upstream error body must not be turned into a model list: a catalog
    invented here would put back a preset the router no longer serves."""
    assert gate.enrich_catalog(b"not json") == b"not json"
    assert gate.enrich_catalog(b'{"error": {"message": "nope"}}') == (
        b'{"error": {"message": "nope"}}'
    )


# ── the wish, and waiting for it ─────────────────────────────────────────────


def test_the_wish_carries_a_correlation_id_and_no_secret(gate, tmp_path):
    held = lease(gate, tmp_path)
    correlation = held.write("acquire", "erweitert", "qwen3.8-27b")
    record = json.loads(pathlib.Path(held.request_path()).read_text("utf-8"))
    assert record["id"] == correlation and len(correlation) == 32
    assert record["holder"] == "pi-web"
    assert record["mode"] == "erweitert"
    assert record["ttl_s"] == gate.LEASE_TTL_S
    assert set(record) == {
        "id",
        "op",
        "mode",
        "model",
        "ttl_s",
        "holder",
        "requested_at",
    }


def test_an_answer_to_an_older_wish_is_not_this_wish(gate, tmp_path):
    """Two sessions asking in sequence share one pair of files; reading the
    previous answer would report a window that was never asked for."""
    held = lease(gate, tmp_path)
    held.write("acquire", "erweitert")
    pathlib.Path(held.status_path()).write_text(
        json.dumps({"id": "stale", "state": "ready"}), "utf-8"
    )
    assert (
        held.status(
            json.loads(pathlib.Path(held.request_path()).read_text("utf-8"))["id"]
        )
        == {}
    )


def test_the_202_is_polled_at_the_cadence_the_broker_names(gate, tmp_path):
    """`retry_after` is how often to look again, not how long the switch takes.
    Sleeping it once and giving up would report a timeout on a window that was
    about to stand."""
    held = lease(gate, tmp_path)
    waited: list[int] = []
    polls = {"n": 0}

    def sleep(seconds):
        polls["n"] += 1
        held.clock.now += seconds
        if polls["n"] == 3:
            answer(held, state="ready", mode="erweitert", expires_at=1_000_900)

    held.sleep = sleep
    pathlib.Path(held.dir).mkdir(parents=True, exist_ok=True)
    record = held.acquire("erweitert", on_wait=waited.append)
    assert record["state"] == "ready"
    assert polls["n"] == 3
    assert held.mode == "erweitert"


def test_the_session_is_told_it_is_waiting_rather_than_left_silent(gate):
    first = gate.waiting_notice("erweitert", "qwen3.8-27b", 0)
    assert "Erweitert" in first and "qwen3.8-27b" in first
    later = gate.waiting_notice("foundry", "gemma-4-12b", 45)
    assert "Foundry" in later and "45" in later


def test_the_wait_reaches_the_session_as_assistant_text(gate):
    """A streamed answer is the only surface a waiting session has; the notice
    has to be a chunk Pi renders, not a log line nobody opens."""
    frame = gate.sse_notice("Moment.\n", "qwen3.8-27b", 1_000_000)
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    payload = json.loads(frame[len(b"data: ") :].decode("utf-8"))
    assert payload["choices"][0]["delta"]["content"] == "Moment.\n"
    assert payload["object"] == "chat.completion.chunk"


def test_a_stream_this_gate_closes_itself_carries_a_finish_reason(gate):
    """Measured on the box 19.9.: a session that ran into a refusal printed
    `Stream ended without finish_reason` and not one word of the German
    sentence the gate had just streamed it. A client that never sees a
    `finish_reason` throws the whole answer away, so the last chunk of a stream
    this gate wrote itself has to carry one."""
    frame = gate.sse_chunk({}, "stop", "qwen3.8-27b", 1_000_000)
    payload = json.loads(frame[len(b"data: ") :].decode("utf-8"))
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["choices"][0]["delta"] == {}
    # The wait notices stay open-ended — only the closing chunk finishes.
    notice = json.loads(
        gate.sse_notice("Moment.\n", "qwen3.8-27b", 1_000_000)[len(b"data: ") :]
    )
    assert notice["choices"][0]["finish_reason"] is None


def test_a_switch_that_never_finishes_ends_in_an_answer_and_not_a_hang(gate, tmp_path):
    held = lease(gate, tmp_path)
    pathlib.Path(held.dir).mkdir(parents=True, exist_ok=True)
    record = held.acquire("erweitert")
    assert record["state"] == "timeout"
    assert held.clock.now >= 1_000_000 + gate.WAIT_DEADLINE_S


# ── renew, release, and the card coming back ─────────────────────────────────


def test_a_live_session_renews_before_the_grace_runs_out(gate, tmp_path):
    held = lease(gate, tmp_path)
    pathlib.Path(held.dir).mkdir(parents=True, exist_ok=True)
    answer_ready(held, gate)
    held.clock.now += gate.RENEW_AFTER_S
    held.touch()
    held.tick()
    record = json.loads(pathlib.Path(held.request_path()).read_text("utf-8"))
    assert record["op"] == "acquire" and record["mode"] == "erweitert"
    assert held.mode == "erweitert"


def test_a_long_generation_still_renews_while_nothing_new_arrives(gate, tmp_path):
    """The renewal cannot hang off incoming requests alone: one answer can take
    longer than the grace, and the box would take the card mid-sentence."""
    held = lease(gate, tmp_path)
    pathlib.Path(held.dir).mkdir(parents=True, exist_ok=True)
    answer_ready(held, gate)
    held.in_flight = 1
    held.clock.now += gate.IDLE_RELEASE_S + gate.RENEW_AFTER_S
    held.tick()
    record = json.loads(pathlib.Path(held.request_path()).read_text("utf-8"))
    assert record["op"] == "acquire"


def test_an_idle_pod_gives_the_card_back(gate, tmp_path):
    held = lease(gate, tmp_path)
    pathlib.Path(held.dir).mkdir(parents=True, exist_ok=True)
    answer_ready(held, gate)
    held.clock.now += gate.IDLE_RELEASE_S + 1
    held.tick()
    record = json.loads(pathlib.Path(held.request_path()).read_text("utf-8"))
    assert record["op"] == "release" and record["holder"] == "pi-web"
    assert held.mode == ""


def test_a_pod_that_holds_nothing_asks_for_nothing(gate, tmp_path):
    held = lease(gate, tmp_path)
    held.clock.now += gate.IDLE_RELEASE_S * 10
    held.tick()
    assert not pathlib.Path(held.request_path()).exists()


def test_a_dead_gate_loses_the_mode_to_the_grace_and_not_to_the_ttl(gate, llama):
    """#1361: the box arms its release at two missed renewals. The TTL this pod
    asks for has to make that a sane number — 900 s means renew every 300 s and
    the card back 600 s after the last renewal, rather than a quarter of an hour
    of a household served by the wrong model.
    """
    assert llama.renew_after(gate.LEASE_TTL_S) == 300
    assert gate.RENEW_AFTER_S == llama.renew_after(gate.LEASE_TTL_S)
    assert llama.expiry_wake(gate.LEASE_TTL_S) == 600
    assert llama.LEASE_GRACE_FACTOR * gate.RENEW_AFTER_S == 600
    # The ordinary end of a window is the gate giving it back, not the box
    # taking it: the idle release has to come first.
    assert gate.IDLE_RELEASE_S < llama.expiry_wake(gate.LEASE_TTL_S)


def test_the_old_permanent_unit_stays_retired(pd):
    """#1392 removed `pi-web-model-lease.service` because it was bound to a pod
    that runs 24/7. The new unit is a different thing with the same purpose —
    the retirement of the old one must not be undone by it."""
    assert pd.LEASE_UNIT == "pi-web-model-lease"
    assert pd.BROKER_UNIT != pd.LEASE_UNIT
    source = (PI_WEB / "post-deploy.py").read_text(encoding="utf-8")
    assert "retire_lease_unit" in source
    assert "release_own_lease" in source


# ── somebody else's window ───────────────────────────────────────────────────


def test_a_window_somebody_else_holds_is_reported_and_never_taken(gate):
    body = gate.held_body(
        "qwen3.8-27b", "erweitert", {"holder": "widget", "expires_at": 1_000_000_000}
    )
    message = body["error"]["message"]
    assert "widget" in message
    assert "Modell-Kachel" in message
    assert body["error"]["holder"] == "widget"


def test_a_held_window_without_an_end_time_still_says_so(gate):
    body = gate.held_body("gemma-4-12b", "foundry", {"holder": "foundry-chronicle"})
    assert "unbestimmte Zeit" in body["error"]["message"]


def test_the_broker_reads_a_409_as_held_rather_than_as_a_failure(pd):
    assert pd.acquire_outcome(409, {"reason": "held", "holder": "widget"}) == "held"
    assert pd.acquire_outcome(200, {"state": "ready"}) == "ready"
    assert pd.acquire_outcome(202, {"state": "preparing"}) == "preparing"
    assert pd.acquire_outcome(400, {"reason": "invalid_model"}) == "error"
    assert pd.acquire_outcome(0, {}) == "error"


def test_the_broker_never_steals_a_window_it_was_refused(pd, monkeypatch):
    calls: list[tuple] = []

    def fake(url, payload=None, method="GET", timeout=10.0):
        calls.append((method, payload))
        return 409, {"holder": "widget", "expires_at": 1_000_000_000}

    monkeypatch.setattr(pd, "http_request", fake)
    record = pd.broker_acquire(
        "http://127.0.0.1:8787/api/model-lease", "c1", "erweitert", 900
    )
    assert record["state"] == "held" and record["holder"] == "widget"
    assert [m for m, _ in calls] == ["POST"]


def test_the_broker_waits_out_a_release_instead_of_sending_a_second_delete(
    pd, monkeypatch
):
    """#1364: the host finishes the release whether anyone polls or not, and the
    contract says a consumer waits for `none`."""
    calls: list[str] = []
    states = iter(["releasing", "releasing", "none"])

    def fake(url, payload=None, method="GET", timeout=10.0):
        calls.append(method)
        if method == "DELETE":
            return 200, {"ok": True, "state": "releasing", "retry_after": 30}
        return 200, {"state": next(states), "retry_after": 30}

    monkeypatch.setattr(pd, "http_request", fake)
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    record = pd.broker_release(
        "http://127.0.0.1:8787/api/model-lease", "c2", "erweitert"
    )
    assert record["state"] == "released"
    assert calls.count("DELETE") == 1


def test_the_broker_polls_a_202_until_the_window_stands(pd, monkeypatch):
    states = iter(["preparing", "preparing", "ready"])
    slept: list[float] = []

    def fake(url, payload=None, method="GET", timeout=10.0):
        if method == "POST":
            return 202, {"state": "preparing", "retry_after": 30}
        return 200, {
            "state": next(states),
            "retry_after": 30,
            "alias": "qwen3.8-27b",
            "expires_at": 1_000_900,
        }

    monkeypatch.setattr(pd, "http_request", fake)
    monkeypatch.setattr(pd.time, "sleep", slept.append)
    record = pd.broker_acquire(
        "http://127.0.0.1:8787/api/model-lease", "c3", "erweitert", 900
    )
    assert record["state"] == "ready" and record["alias"] == "qwen3.8-27b"
    assert slept == [30.0, 30.0, 30.0]


def test_retry_after_is_a_cadence_and_never_a_one_shot_wait(pd):
    assert pd.poll_cadence({"retry_after": 30}) == 30.0
    assert pd.poll_cadence({}) == 5.0
    assert pd.poll_cadence({"retry_after": True}) == 5.0
    assert pd.poll_cadence({"retry_after": -1}) == 5.0


def test_a_wish_already_answered_is_not_run_twice(pd, tmp_path, monkeypatch):
    """A `.path` unit can fire more than once for one write. Re-running an
    `acquire` re-arms the window, and re-running a `release` is the second
    DELETE the contract forbids."""
    data_dir = str(tmp_path)
    pathlib.Path(pd.lease_dir(data_dir)).mkdir(parents=True)
    pathlib.Path(pd.request_path(data_dir)).write_text(
        json.dumps({"id": "c4", "op": "acquire", "mode": "erweitert", "ttl_s": 900}),
        "utf-8",
    )
    pathlib.Path(pd.status_path(data_dir)).write_text(
        json.dumps({"id": "c4", "state": "ready"}), "utf-8"
    )

    def fake(*args, **kwargs):
        raise AssertionError("the lease API was called for an answered wish")

    monkeypatch.setattr(pd, "http_request", fake)
    assert pd.broker_run(data_dir, "8787") == 0


# ── the namespace boundary ───────────────────────────────────────────────────


def test_nothing_in_the_pod_ever_calls_the_engine_directly(gate, pod):
    """`/api/model-lease` is loopback-only and unauthenticated; this pod has its
    own netns. A call to 8787 from in here cannot work, and the whole file
    bridge exists so that nobody writes one."""
    in_pod = sorted((ROOT / "pi-web").rglob("*.py"))
    in_pod += sorted((ROOT / "pi-web" / "plugins").rglob("*.js"))
    assert len(in_pod) >= 6
    for path in in_pod:
        code = _code(path)
        for banned in (":8787", "api/model-lease", "CHAT_PORT"):
            assert banned not in code, f"{path.name}: {banned}"
    # And no container is handed the Engine's port to begin with: CHAT_PORT is
    # a host-side variable the post-deploy reads, never pod environment.
    for container in pod["spec"]["containers"] + pod["spec"]["initContainers"]:
        for entry in container.get("env") or []:
            assert "CHAT_PORT" not in entry["name"]
            assert "8787" not in str(entry.get("value", ""))
    assert "model-lease" in gate.DEFAULT_LEASE_DIR


def test_the_gate_and_the_broker_agree_on_where_the_files_are(gate, pd):
    """The pod sees /data, the host sees the hostPath behind it. Two names for
    one directory is exactly the kind of thing that drifts."""
    assert gate.DEFAULT_LEASE_DIR.endswith("/" + pd.LEASE_DIR_NAME)
    assert gate.REQUEST_FILE == pd.LEASE_REQUEST_FILE
    assert gate.STATUS_FILE == pd.LEASE_STATUS_FILE
    assert pd.lease_dir("/mnt/data/stacks") == (
        "/mnt/data/stacks/pi-web/data/model-lease"
    )
    assert gate.HOLDER == pd.LEASE_HOLDER


def answer_ready(held, gate) -> None:
    """Put the lease into the state a granted window leaves behind."""
    held.write("acquire", "erweitert")
    answer(held, state="ready", mode="erweitert")
    held.mode = "erweitert"
    held.last_renew = held.clock()
    held.last_seen = held.clock()
