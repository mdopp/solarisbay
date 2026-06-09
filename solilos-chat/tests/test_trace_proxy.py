"""Trace-proxy capture/summary logic (permanent LLM traceability).

Pure-function coverage of the request/response summarisers and the per-block
token split — the parts that turn a raw Ollama call into a per-turn trace record.
"""

from __future__ import annotations

import json

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
