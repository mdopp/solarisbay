"""The everyday-chat model preference store (JSON sidecar next to solilos.db)."""

from __future__ import annotations

import pytest

from solilos_chat import settings_store


def _db(tmp_path) -> str:
    return str(tmp_path / "solilos.db")


def test_default_is_thorough_when_unset(tmp_path):
    assert settings_store.get_other_model_pref(_db(tmp_path)) == "thorough"


def test_roundtrip_fast(tmp_path):
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, "fast")
    assert settings_store.get_other_model_pref(db) == "fast"


def test_roundtrip_thorough(tmp_path):
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, "thorough")
    assert settings_store.get_other_model_pref(db) == "thorough"


def test_invalid_persisted_value_falls_back_to_default(tmp_path):
    db = _db(tmp_path)
    (tmp_path / "app_settings.json").write_text('{"other_model_pref": "12b"}', "utf-8")
    assert settings_store.get_other_model_pref(db) == "thorough"


def test_set_rejects_invalid_value(tmp_path):
    with pytest.raises(ValueError):
        settings_store.set_other_model_pref(_db(tmp_path), "12b")


# --- Household-profile model override (#366) ------------------------------


def test_household_model_empty_when_unset(tmp_path):
    assert settings_store.get_household_model(_db(tmp_path)) == ""


def test_household_model_roundtrip(tmp_path):
    db = _db(tmp_path)
    settings_store.set_household_model(db, "gemma4:12b")
    assert settings_store.get_household_model(db) == "gemma4:12b"


def test_household_model_coexists_with_other_pref(tmp_path):
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, "fast")
    settings_store.set_household_model(db, "gemma4:12b")
    # Both keys live in the one sidecar; neither write clobbers the other.
    assert settings_store.get_other_model_pref(db) == "fast"
    assert settings_store.get_household_model(db) == "gemma4:12b"


# --- Global TTS voice (#368) ----------------------------------------------


def test_tts_voice_empty_when_unset(tmp_path):
    assert settings_store.get_tts_voice(_db(tmp_path)) == ""


def test_tts_voice_roundtrip(tmp_path):
    db = _db(tmp_path)
    settings_store.set_tts_voice(db, "anna")
    assert settings_store.get_tts_voice(db) == "anna"


def test_tts_voice_coexists_with_models(tmp_path):
    db = _db(tmp_path)
    settings_store.set_household_model(db, "gemma4:12b")
    settings_store.set_tts_voice(db, "anna")
    # All keys share the one sidecar; neither write clobbers the other.
    assert settings_store.get_household_model(db) == "gemma4:12b"
    assert settings_store.get_tts_voice(db) == "anna"
