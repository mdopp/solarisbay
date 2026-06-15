"""Tests for chat reasoning-effort selection (#222 / #224)."""

from __future__ import annotations

import pytest

from solaris_chat import reasoning


@pytest.mark.parametrize(
    "text",
    [
        "welche Lichter sind an",
        "mach das Licht aus",
        "wie spät ist es",
        "",
    ],
)
def test_default_is_fast(text):
    assert reasoning.choose_effort(text) == reasoning.FAST


@pytest.mark.parametrize(
    "text",
    [
        "denk mal scharf nach",
        "erkläre mir das genau",
        "think it through",
        "explain this in detail",
    ],
)
def test_cue_escalates(text):
    assert reasoning.choose_effort(text) == reasoning.HIGH


def test_admin_escalates_without_cue():
    assert reasoning.choose_effort("status?", admin=True) == reasoning.HIGH


@pytest.mark.parametrize("value", ["none", "low", "high"])
def test_selector_overrides_everything(value):
    # The selector wins over both the adaptive default and an admin context.
    assert reasoning.choose_effort("hi", selector=value) == value
    assert reasoning.choose_effort("hi", selector=value, admin=True) == value
    # Even an explicit cue is overridden by an explicit selector choice.
    assert reasoning.choose_effort("denk nach", selector="none") == reasoning.FAST


@pytest.mark.parametrize("bad", ["", "ultra", None, 5, "None", "HIGH"])
def test_unknown_selector_falls_back_to_adaptive(bad):
    # A junk selector value is ignored; the adaptive default applies.
    assert reasoning.normalize_selector(bad) is None
    assert reasoning.choose_effort("welche Lichter sind an", selector=bad) == (
        reasoning.FAST
    )


# model selection (model_for_effort) was removed: the household is fixed to the
# fast model per profile (#293), and thinking follows the model — the fast model
# is suppressed by the trace proxy, the thorough model (admin/other tasks) keeps
# reasoning. There is no per-effort model routing on the household path.
