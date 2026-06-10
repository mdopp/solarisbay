"""Trace-proxy capture/summary logic (permanent LLM traceability).

Pure-function coverage of the request/response summarisers and the per-block
token split — the parts that turn a raw Ollama call into a per-turn trace record.
"""

from __future__ import annotations

import json

import pytest

from solilos_chat import trace_proxy as tp


def test_summarize_request_splits_blocks_and_tools():
    body = json.dumps(
        {
            "model": "gemma4:e2b",
            "stream": True,
            "options": {"num_ctx": 131072},
            "messages": [
                {"role": "system", "content": "x" * 200},
                {"role": "user", "content": "Welche Lichter sind an?"},
            ],
            "tools": [
                {"function": {"name": "ha_list_entities", "parameters": {}}},
                {
                    "function": {
                        "name": "mcp_servicebay_mcp_reboot_node",
                        "parameters": {},
                    }
                },
            ],
        }
    ).encode()
    s = tp.summarize_request(body)
    assert s["model"] == "gemma4:e2b"
    assert s["stream"] is True
    assert s["num_ctx"] == 131072
    assert s["blocks_chars"]["system"] == 200
    assert s["blocks_chars"]["user"] == len("Welche Lichter sind an?")
    assert s["n_tools"] == 2
    assert {t["name"] for t in s["tools"]} == {
        "ha_list_entities",
        "mcp_servicebay_mcp_reboot_node",
    }
    assert all(t["tok_est"] > 0 for t in s["tools"])
    # No `Active Hermes profile:` line in this system block → untagged.
    assert s["profile"] is None


def test_extract_profile_reads_system_line():
    msgs = [
        {
            "role": "system",
            "content": "...\nActive Hermes profile: household. Other...",
        },
        {"role": "user", "content": "hi"},
    ]
    assert tp.extract_profile(msgs) == "household"


def test_extract_profile_absent_line_returns_none():
    msgs = [{"role": "system", "content": "no profile here"}]
    assert tp.extract_profile(msgs) is None


def test_summarize_request_carries_profile():
    body = json.dumps(
        {
            "model": "gemma4:e2b",
            "messages": [
                {"role": "system", "content": "Active Hermes profile: admin. x"},
                {"role": "user", "content": "hi"},
            ],
        }
    ).encode()
    s = tp.summarize_request(body)
    assert s["profile"] == "admin"
    rec = tp.build_record("/api/chat", s, {"usage": None}, 1.0)
    assert rec["profile"] == "admin"


def test_thinking_policy_fast_turn_suppresses_reasoning(monkeypatch):
    # No sentinel on the current turn → suppress the hidden reasoning block.
    monkeypatch.setattr(tp, "NOTHINK_MODELS", {"gemma4:e2b"})
    out = tp.apply_thinking_policy(
        json.dumps(
            {"model": "gemma4:e2b", "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
    )
    assert json.loads(out)["reasoning_effort"] == "none"


def test_thinking_policy_thinking_turn_allows_and_strips_sentinel(monkeypatch):
    # Sentinel on the current turn → leave it to reason, and strip the carrier.
    monkeypatch.setattr(tp, "NOTHINK_MODELS", {"gemma4:e2b"})
    body = json.dumps(
        {
            "model": "gemma4:e2b",
            "messages": [{"role": "user", "content": f"denk nach {tp.THINK_SENTINEL}"}],
        }
    ).encode()
    d = json.loads(tp.apply_thinking_policy(body))
    assert "reasoning_effort" not in d  # not suppressed → model reasons
    assert tp.THINK_SENTINEL not in d["messages"][0]["content"]  # carrier stripped
    assert d["messages"][0]["content"] == "denk nach"


def test_thinking_policy_decision_is_current_turn_not_history(monkeypatch):
    # A past thinking turn's sentinel in history must NOT make a fast turn reason.
    monkeypatch.setattr(tp, "NOTHINK_MODELS", {"gemma4:e2b"})
    body = json.dumps(
        {
            "model": "gemma4:e2b",
            "messages": [
                {"role": "user", "content": f"alte frage {tp.THINK_SENTINEL}"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "schnelle frage"},
            ],
        }
    ).encode()
    d = json.loads(tp.apply_thinking_policy(body))
    assert d["reasoning_effort"] == "none"  # current (last) turn is fast
    assert all(tp.THINK_SENTINEL not in m["content"] for m in d["messages"])


def test_thinking_policy_leaves_thorough_model_and_explicit_choice(monkeypatch):
    monkeypatch.setattr(tp, "NOTHINK_MODELS", {"gemma4:e2b"})
    thorough = json.dumps({"model": "gemma4:12b", "messages": []}).encode()
    assert tp.apply_thinking_policy(thorough) == thorough
    explicit = json.dumps(
        {
            "model": "gemma4:e2b",
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    assert json.loads(tp.apply_thinking_policy(explicit))["reasoning_effort"] == "high"


def test_thinking_policy_fails_open_on_non_json(monkeypatch):
    monkeypatch.setattr(tp, "NOTHINK_MODELS", {"gemma4:e2b"})
    assert tp.apply_thinking_policy(b"not json") == b"not json"


def test_summarize_response_json_tool_call():
    body = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [{"function": {"name": "ha_list_entities"}}]
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20814,
                "completion_tokens": 265,
                "total_tokens": 21079,
            },
        }
    ).encode()
    s = tp.summarize_response(body)
    assert s["usage"]["prompt_tokens"] == 20814
    assert s["finish_reason"] == "tool_calls"
    assert s["tool_calls"] == ["ha_list_entities"]


def test_summarize_response_sse_stream_last_usage():
    sse = (
        'data: {"choices":[{"delta":{"content":"Das"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":21547,"completion_tokens":9,"total_tokens":21556}}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    s = tp.summarize_response(sse)
    assert s["usage"]["prompt_tokens"] == 21547
    assert s["finish_reason"] == "stop"


def test_build_record_splits_prompt_tokens_proportionally():
    req = {
        "model": "gemma4:e2b",
        "stream": True,
        "num_ctx": 131072,
        "blocks_chars": {"system": 21263, "user": 23},
        "tools_chars": 70374,
        "n_tools": 76,
        "tools": [{"name": "ha_list_entities", "chars": 100, "tok_est": 25}],
    }
    resp = {
        "usage": {"prompt_tokens": 20814, "completion_tokens": 265},
        "finish_reason": "tool_calls",
        "tool_calls": ["ha_list_entities"],
    }
    rec = tp.build_record("/v1/chat/completions", req, resp, 4.316)
    # The per-block + tools token figures sum (±1 rounding) to the real total.
    summed = sum(rec["blocks_tok"].values()) + rec["tools_tok"]
    assert abs(summed - 20814) <= 2
    # Tools dominate the prompt (~85% — the headline finding).
    assert rec["tools_tok"] > rec["blocks_tok"]["system"] * 2
    assert rec["context_free"] == 131072 - 20814
    assert rec["wall_s"] == 4.316
    assert rec["tool_calls"] == ["ha_list_entities"]


def test_build_record_without_usage_falls_back_to_char_estimate():
    req = {
        "blocks_chars": {"system": 400, "user": 20},
        "tools_chars": 800,
        "n_tools": 3,
        "num_ctx": None,
    }
    rec = tp.build_record("/v1/chat/completions", req, {"usage": None}, 1.0)
    assert rec["prompt_tokens"] is None
    assert rec["context_free"] is None
    assert rec["tools_tok"] == round(800 / 4)


@pytest.fixture
def fresh_store(monkeypatch):
    """Isolate the module-level ring buffers + id counter per test."""
    from collections import deque

    monkeypatch.setattr(tp, "_traces", deque(maxlen=tp.RING))
    monkeypatch.setattr(tp, "_details", {})
    monkeypatch.setattr(tp, "_next_id", 0)


def test_detail_request_keeps_exact_content():
    body = json.dumps(
        {
            "model": "gemma4:e2b",
            "messages": [
                {"role": "system", "content": "x" * 200},
                {"role": "user", "content": "Welche Lichter sind an?"},
            ],
            "tools": [
                {"function": {"name": "ha_list_entities", "parameters": {"a": 1}}},
            ],
        }
    ).encode()
    d = tp.detail_request(body)
    assert d["model"] == "gemma4:e2b"
    assert d["messages"][0]["content"] == "x" * 200
    assert d["messages"][1]["content"] == "Welche Lichter sind an?"
    # Full tool definition retained verbatim, not collapsed to a size.
    assert d["tools"][0]["function"]["parameters"] == {"a": 1}


def test_detail_response_json_final_and_tool_calls():
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "Das Sofalicht ist an.",
                        "tool_calls": [{"function": {"name": "ha_list_entities"}}],
                    }
                }
            ]
        }
    ).encode()
    d = tp.detail_response(body)
    assert d["final"] == "Das Sofalicht ist an."
    assert d["tool_calls"][0]["function"]["name"] == "ha_list_entities"


def test_detail_response_sse_reassembles_final_text():
    sse = (
        'data: {"choices":[{"delta":{"content":"Das "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Sofalicht "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"ist an."}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    d = tp.detail_response(sse)
    assert d["final"] == "Das Sofalicht ist an."


def test_store_trace_assigns_stable_ids_and_keeps_list_light(fresh_store):
    id0 = tp.store_trace({"path": "/api/chat"}, {"request": {"big": "x" * 1000}})
    id1 = tp.store_trace({"path": "/api/chat"}, {"request": {"big": "y" * 1000}})
    assert (id0, id1) == (0, 1)
    # The light list carries the id but not the full detail body.
    light = list(tp._traces)
    assert [r["id"] for r in light] == [0, 1]
    assert "request" not in light[0]
    # Detail is fetchable by id.
    assert tp._details[0]["request"]["big"] == "x" * 1000
    assert tp._details[1]["request"]["big"] == "y" * 1000


def test_store_trace_caps_detail_fifo(fresh_store, monkeypatch):
    monkeypatch.setattr(tp, "DETAIL_RING", 3)
    ids = [tp.store_trace({"path": "/api/chat"}, {"n": i}) for i in range(5)]
    # Only the last 3 details are retained; oldest evicted FIFO.
    assert sorted(tp._details) == ids[-3:]
    assert 0 not in tp._details and 1 not in tp._details
    assert tp._details[ids[-1]] == {"n": 4}
