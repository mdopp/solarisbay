"""Per-description token budget (tools/__init__.py: ~100–200 tokens each, #643).

The six music/enrollment tool descriptions were trimmed once their dialog scripts
moved into structured `say` results; this guards them from creeping back over the
~200-token ceiling. Builders do no I/O at build time, so dummy args suffice — the
~4-chars/token convention (store.truncate_session_head) is the estimator.
"""

from __future__ import annotations

from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.media import build_media_tools
from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.radio import build_radio_tools
from solaris_chat.engine.tools.register import build_register_tools

_BUDGET_TOKENS = 200


class _DummyJellyfin:
    async def stream_url(self, audio_id, static=True):  # pragma: no cover
        return ""

    async def lyrics(self, audio_id):  # pragma: no cover
        return None


def _trimmed_tools():
    tools = []
    tools += build_radio_tools("/tmp/notes", "http://ha", "tok", lambda: "household")
    tools += build_music_query_tools(
        "/tmp/db.sqlite",
        lambda: "household",
        _DummyJellyfin(),
        hass_url="http://ha",
        hass_token="tok",
    )
    tools += build_media_tools("http://ha", "tok")
    tools += build_choice_tools()
    tools += build_register_tools("/tmp/db.sqlite")
    return {t.name: t for t in tools}


def test_trimmed_descriptions_within_budget():
    by_name = _trimmed_tools()
    for name in (
        "play_music",
        "play_radio",
        "media_find_podcast",
        "music_query",
        "offer_choices",
        "start_voice_enrollment",
    ):
        assert name in by_name, f"{name} not built"
        est = len(by_name[name].description) // 4
        assert est <= _BUDGET_TOKENS, (
            f"{name} description ~{est} tok > {_BUDGET_TOKENS}"
        )
