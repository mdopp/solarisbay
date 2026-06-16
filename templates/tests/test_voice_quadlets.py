"""Tests for the Solaris-owned voice-pipeline Quadlet rendering (#456).

Solaris owns its whole voice pipeline: whisper STT, the Kokoro-Martin TTS and
the wyoming bridge run as companion `.container` Quadlets the post-deploy
writes (GPU via CDI, #1026), and the openWakeWord wake engine. The
render_* functions are pure, so they're unit-tested directly (mirroring the
ServiceBay voice template's own quadlet-render tests)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

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
    return _load("solaris_pd_quadlets", TEMPLATES / "solaris" / "post-deploy.py")


# -- whisper -----------------------------------------------------------------


def test_whisper_gpu_unit_has_cdi_device_and_selinux_relax(pd):
    unit = pd.render_whisper_unit("/mnt/data", "medium-int8", "de", gpu=True)
    # #1026: CDI device must be AddDevice= on the quadlet, never resources.limits.
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit
    assert "Image=lscr.io/linuxserver/faster-whisper:gpu" in unit
    assert "Environment=WHISPER_MODEL=medium-int8" in unit
    assert "Environment=WHISPER_LANG=de" in unit
    assert "Network=host" in unit
    # GPU image keeps its model cache under /config.
    assert "Volume=/mnt/data/voice/whisper-gpu:/config:Z" in unit


def test_whisper_cpu_unit_uses_cpu_image_and_wyoming_port(pd):
    unit = pd.render_whisper_unit("/mnt/data", "base-int8", "de", gpu=False)
    assert "AddDevice" not in unit
    assert "SecurityLabelDisable" not in unit
    assert "Image=docker.io/rhasspy/wyoming-whisper:latest" in unit
    # Same Wyoming endpoint as GPU (the linuxserver image binds :10300 itself).
    assert "--uri tcp://0.0.0.0:10300" in unit
    assert "--model base-int8 --language de" in unit
    assert "Volume=/mnt/data/voice/whisper:/data:Z" in unit


def test_install_whisper_unit_picks_gpu_model_default_on_cdi(pd, monkeypatch, tmp_path):
    rendered = {}
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(pd, "env", lambda key, default="": default)
    monkeypatch.setattr(
        pd,
        "render_whisper_unit",
        lambda data_dir, model, language, gpu: (
            rendered.update(model=model, gpu=gpu) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    # base-int8 default + GPU box ⇒ auto-upgrade to medium-int8.
    assert rendered == {"model": "medium-int8", "gpu": True}
    assert (tmp_path / "voice" / "whisper-gpu").is_dir()


def test_install_whisper_unit_keeps_explicit_model_on_cpu(pd, monkeypatch, tmp_path):
    rendered = {}
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "env",
        lambda key, default="": "small-int8" if key == "WHISPER_MODEL" else default,
    )
    monkeypatch.setattr(
        pd,
        "render_whisper_unit",
        lambda data_dir, model, language, gpu: (
            rendered.update(model=model, gpu=gpu) or "UNIT"
        ),
    )
    monkeypatch.setattr(pd, "install_unit", lambda unit, content: True)
    assert pd.install_whisper_unit(str(tmp_path)) is True
    assert rendered == {"model": "small-int8", "gpu": False}
    assert (tmp_path / "voice" / "whisper").is_dir()


# -- Kokoro-Martin TTS + bridge ----------------------------------------------


def test_tts_unit_is_solaris_image_martin_voice_with_cdi(pd):
    unit = pd.render_tts_unit()
    # The RENAMED bundled image, not solilos-tts.
    assert "Image=ghcr.io/mdopp/solaris-tts:latest" in unit
    assert "Environment=KOKORO_ONNX_VOICE=martin" in unit
    assert "Environment=KOKORO_ONNX_LANG=de" in unit
    assert "Environment=KOKORO_ONNX_PROVIDER=cuda" in unit
    assert "AddDevice=nvidia.com/gpu=all" in unit
    assert "SecurityLabelDisable=true" in unit


def test_tts_bridge_unit_fronts_kokoro_on_wyoming_port(pd):
    unit = pd.render_tts_bridge_unit()
    assert "Image=ghcr.io/roryeckel/wyoming_openai:latest" in unit
    assert "--uri tcp://0.0.0.0:10203" in unit
    assert "--languages de" in unit
    assert "--tts-openai-url http://127.0.0.1:8881/v1" in unit
    assert "--tts-models kokoro" in unit
    assert "--tts-streaming-models kokoro" in unit
    assert "--tts-backend KOKORO_FASTAPI" in unit
    # The bridge waits for the TTS unit.
    assert f"After=network-online.target {pd.TTS_UNIT}.service" in unit
    # The bridge is a CPU shim — never claim the GPU on it.
    assert "AddDevice" not in unit


# -- openWakeWord ------------------------------------------------------------


def test_openwakeword_unit_scans_custom_dir_on_wyoming_port(pd):
    unit = pd.render_openwakeword_unit("/mnt/data/voice/custom")
    assert "Image=docker.io/rhasspy/wyoming-openwakeword:latest" in unit
    assert f"ContainerName={pd.OPENWAKEWORD_UNIT}" in unit
    assert "--uri tcp://0.0.0.0:10400" in unit
    assert "--custom-model-dir /custom_models" in unit
    assert "Volume=/mnt/data/voice/custom:/custom_models:Z" in unit
    assert "Network=host" in unit
    # CPU service — the model is tiny, never claim the GPU.
    assert "AddDevice" not in unit


def test_install_openwakeword_creates_dir_and_writes_unit(pd, monkeypatch, tmp_path):
    custom = tmp_path / "voice" / "custom"
    wrote = {}

    def _install(name, content):
        wrote["n"] = name
        return True

    monkeypatch.setattr(pd, "install_unit", _install)
    assert pd.install_openwakeword_unit(str(custom)) is True
    assert custom.is_dir()
    assert wrote["n"] == pd.OPENWAKEWORD_UNIT


def test_install_tts_units_skips_without_cdi(pd, monkeypatch):
    monkeypatch.setattr(pd, "cdi_available", lambda: False)
    monkeypatch.setattr(
        pd,
        "install_unit",
        lambda *a: pytest.fail("must not write TTS units on CPU box"),
    )
    assert pd.install_tts_units() is False


def test_install_tts_units_writes_both_on_gpu(pd, monkeypatch):
    written = []
    monkeypatch.setattr(pd, "cdi_available", lambda: True)
    monkeypatch.setattr(
        pd, "install_unit", lambda unit, content: written.append(unit) or True
    )
    assert pd.install_tts_units() is True
    assert written == [pd.TTS_UNIT, pd.TTS_BRIDGE_UNIT]


# -- openWakeWord custom-models dir ------------------------------------------


def test_setup_custom_models_dir_creates_path(pd, tmp_path):
    target = tmp_path / "voice" / "custom"
    pd.setup_custom_models_dir(str(target))
    assert target.is_dir()


def test_setup_custom_models_dir_noop_when_empty(pd, tmp_path):
    # Empty/unset ⇒ no dir created (nothing to mount).
    before = set(tmp_path.iterdir())
    pd.setup_custom_models_dir("")
    assert set(tmp_path.iterdir()) == before


# -- install_unit idempotency ------------------------------------------------


def test_install_unit_noop_when_current_and_active(pd, monkeypatch, tmp_path):
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "voice-whisper.container").write_text("CONTENT")
    monkeypatch.setattr(
        pd.os.path,
        "expanduser",
        lambda p: (
            str(tmp_path / ".config" / "containers" / "systemd")
            if "systemd" in p
            else p
        ),
    )
    monkeypatch.setattr(pd, "service_active", lambda unit: True)
    called = []
    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: called.append(a))
    assert pd.install_unit("voice-whisper", "CONTENT") is True
    # No daemon-reload / restart when content matches and service is active.
    assert called == []


def test_install_unit_rewrites_on_drift(pd, monkeypatch, tmp_path):
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    unit_path = systemd_dir / "voice-whisper.container"
    unit_path.write_text("OLD")
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: str(systemd_dir) if "systemd" in p else p
    )
    monkeypatch.setattr(pd, "service_active", lambda unit: True)

    class _OK:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: _OK())
    assert pd.install_unit("voice-whisper", "NEW") is True
    assert unit_path.read_text() == "NEW"
