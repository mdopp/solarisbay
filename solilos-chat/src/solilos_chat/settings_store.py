"""Tiny persisted app-settings store (JSON sidecar next to solilos.db).

The only setting today is the **non-household model preference** (#332-followup):
which of the two gateway-backed models everyday (non-household) chats route to —
`"fast"` (e2b, the household gateway) or `"thorough"` (12b, the sol-deep
gateway). It is a routing toggle, not a Hermes config rewrite, so it lives here
rather than in `config.yaml`: the chat server owns it and reads it per turn.

It rides a JSON file beside `solilos.db` (the same persistent writable volume
`topics_store` uses) so no schema migration is needed. The chat server caches
the value in memory and treats this file as the restart-survival source.
"""

from __future__ import annotations

import json
from pathlib import Path

_VALID = ("fast", "thorough")
DEFAULT_PREF = "thorough"
_KEY = "other_model_pref"
_HOUSEHOLD_KEY = "household_model"
_TTS_VOICE_KEY = "tts_voice"


def _path(db_path: str) -> Path:
    return Path(db_path).parent / "app_settings.json"


def _read(db_path: str) -> dict:
    try:
        data = json.loads(_path(db_path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(db_path: str, key: str, value: str) -> None:
    data = _read(db_path)
    data[key] = value
    p = _path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), "utf-8")


def get_other_model_pref(db_path: str) -> str:
    """The everyday-chat model preference; `DEFAULT_PREF` when unset/invalid."""
    value = _read(db_path).get(_KEY)
    return value if value in _VALID else DEFAULT_PREF


def set_other_model_pref(db_path: str, value: str) -> None:
    """Persist the everyday-chat model preference. Raises ValueError on a bad
    value (callers validate first, so this is a guard, not a code path)."""
    if value not in _VALID:
        raise ValueError(f"other_model_pref must be one of {_VALID}, got {value!r}")
    _write(db_path, _KEY, value)


def get_household_model(db_path: str) -> str:
    """The admin-selected household-profile model override (#366); `""` when
    unset — the caller then falls back to the configured FAST_MODEL default, so
    the fast-only default is preserved for installs that never touch the picker."""
    value = _read(db_path).get(_HOUSEHOLD_KEY)
    return value.strip() if isinstance(value, str) else ""


def set_household_model(db_path: str, value: str) -> None:
    """Persist the household-profile model override (#366). The server validates
    the tag against the offered options before calling this."""
    _write(db_path, _HOUSEHOLD_KEY, value.strip())


def get_tts_voice(db_path: str) -> str:
    """The admin-selected global Kokoro TTS voice (#368); `""` when unset — the
    caller (the post-deploy pipeline wiring) then keeps the Martin default, so
    the existing voice is preserved for installs that never touch the picker."""
    value = _read(db_path).get(_TTS_VOICE_KEY)
    return value.strip() if isinstance(value, str) else ""


def set_tts_voice(db_path: str, value: str) -> None:
    """Persist the global TTS voice (#368). The server validates the voice
    against the offered options before calling this."""
    _write(db_path, _TTS_VOICE_KEY, value.strip())
