"""Pull wrapper + VRAM-headroom estimate + admin gate (#367)."""

from __future__ import annotations

import json

import pytest

from solilos_chat.engine import ollama, vram
from solilos_chat.engine.ollama import OllamaChat, OllamaError

GIB = 1024 * 1024 * 1024


# --- /api/pull streaming wrapper ------------------------------------------


def _patch_ollama_post(monkeypatch, lines, status=200):
    """Stub aiohttp so POST returns `lines` (each a dict) as the ndjson body."""

    class _Content:
        def __aiter__(self):
            async def gen():
                for obj in lines:
                    yield (json.dumps(obj) + "\n").encode()

            return gen()

    class _Resp:
        def __init__(self):
            self.status = status
            self.content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "boom"

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            _Session.last = {"url": url, "json": json}
            return _Resp()

    monkeypatch.setattr(ollama.aiohttp, "ClientSession", _Session)
    return _Session


async def test_pull_builds_request_and_streams_progress(monkeypatch):
    progress = [
        {"status": "pulling manifest"},
        {"status": "downloading", "completed": 50, "total": 100},
        {"status": "success"},
    ]
    sess = _patch_ollama_post(monkeypatch, progress)
    client = OllamaChat("http://x:11434")

    chunks = [c async for c in client.pull("hf.co/owner/repo:Q4_K_M")]

    assert chunks == progress
    assert sess.last["url"] == "http://x:11434/api/pull"
    # The HF repo tag is passed straight to Ollama (stream on, no extra infra).
    assert sess.last["json"] == {"model": "hf.co/owner/repo:Q4_K_M", "stream": True}


async def test_pull_raises_on_error_status(monkeypatch):
    _patch_ollama_post(monkeypatch, [], status=404)
    client = OllamaChat("http://x:11434")
    with pytest.raises(OllamaError):
        [c async for c in client.pull("nope:bad")]


# --- combined-vs-available estimate ---------------------------------------


def test_combined_uses_measured_vram_then_disk_overhead():
    tags = [{"name": "a:1", "size": 2 * GIB}, {"name": "b:1", "size": 4 * GIB}]
    ps = [{"name": "a:1", "size_vram": 3 * GIB}]
    # a:1 loaded -> measured 3 GiB; b:1 disk-only -> 4 GiB * 1.2 overhead.
    out = vram.combined_selected_bytes(["a:1", "b:1"], tags, ps)
    assert out == 3 * GIB + int(4 * GIB * 1.2)


def test_combined_dedupes_and_skips_unpulled_tags():
    tags = [{"name": "a:1", "size": 2 * GIB}]
    out = vram.combined_selected_bytes(["a:1", "a:1", "missing:1"], tags, [])
    assert out == int(2 * GIB * 1.2)  # counted once, unknown tag contributes 0


def test_available_from_env_total_minus_resident(monkeypatch):
    monkeypatch.setenv("GPU_TOTAL_VRAM", str(16 * GIB))
    ps = [{"name": "a:1", "size_vram": 6 * GIB}]
    assert vram.available_bytes(ps) == 10 * GIB


def test_available_unknown_without_env_or_smi(monkeypatch):
    monkeypatch.delenv("GPU_TOTAL_VRAM", raising=False)
    monkeypatch.setattr(vram.shutil, "which", lambda _: None)
    assert vram.available_bytes([]) is None


async def test_servicebay_gpu_sums_resources(monkeypatch):
    import solilos_chat.engine.tools.mcp_tools as mcp

    async def fake_call(url, token_path, name, arguments):
        assert name == "get_system_info"
        return json.dumps(
            {"resources": {"gpus": [{"memoryTotal": 16 * GIB, "memoryUsed": 10 * GIB}]}}
        )

    monkeypatch.setattr(mcp, "call_sb_tool", fake_call)
    assert await vram.servicebay_gpu("http://sb", "/tok") == (16 * GIB, 10 * GIB)


async def test_servicebay_gpu_none_when_no_url_or_no_gpu(monkeypatch):
    assert await vram.servicebay_gpu("", "/tok") is None

    import solilos_chat.engine.tools.mcp_tools as mcp

    async def no_gpu(url, token_path, name, arguments):
        return json.dumps({"resources": {"gpus": []}})

    monkeypatch.setattr(mcp, "call_sb_tool", no_gpu)
    assert await vram.servicebay_gpu("http://sb", "/tok") is None
