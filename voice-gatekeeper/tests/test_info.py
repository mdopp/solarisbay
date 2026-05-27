"""Regression test for #1024 — gatekeeper Wyoming Info advertisement.

Wyoming's `AsrProgram` / `AsrModel` / `TtsProgram` dataclasses gained a
required `version` field between 1.5 and 1.9. The gatekeeper's
constructor calls in `__main__._info()` omitted it for 1.5.x, which was
backward-compatible at the time but became a TypeError on every
satellite connection once a fresh image rebuilt against 1.9.x.

This test imports + invokes `_info()` end-to-end so any future change
to those Wyoming dataclasses (a renamed field, a new required arg) is
caught by `pytest` before the image ships.
"""

from __future__ import annotations


def test_info_constructs_without_error() -> None:
    """Crashes pre-#1024 with `TypeError: AsrModel.__init__() missing 1
    required positional argument: 'version'`."""
    from gatekeeper.__main__ import _info

    info = _info()
    assert info.asr, "_info must advertise at least one ASR program"
    assert info.tts, "_info must advertise at least one TTS program"


def test_info_versions_populated() -> None:
    """Every advertised program/model carries a non-empty `version`
    so satellites can pin against gatekeeper releases. Catches a
    future refactor that drops the field back to None."""
    from gatekeeper.__main__ import _info

    info = _info()
    for program in info.asr:
        assert program.version, f"AsrProgram {program.name} missing version"
        for model in program.models:
            assert model.version, f"AsrModel {model.name} missing version"
    for program in info.tts:
        assert program.version, f"TtsProgram {program.name} missing version"
