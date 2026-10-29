"""The design brief is assembled by script from the board repo's docs
(dv-9133 phase 2), never by hand.

Contract (docs/research/dv-9133/analysis.md, phase 2 step 3):
``src.designloop.brief.build_brief(repo, screen_id, change_request) -> Brief``
reads ``docs/features.yaml`` (must-keep), ``docs/SCREENS.md`` (the screen's
``###`` passport + §1 tokens), ``docs/screens/<screen_id>/*-{dark,light}.png``
(the current screen, "before") and ``docs/UI_GUIDE.md`` (rules). Pure file
reading. ``Brief.text`` is the brief as prompt text; ``Brief.refs`` the
reference PNG paths.
"""

from pathlib import Path

from tests.unit.test_designloop.helpers import (
    FEED_ACTIONS,
    FEED_API,
    FEED_FEATURES,
    REFS,
    ref,
)

CHANGE = "Too many controls above the first card; make the feed calmer."


def test_feed_brief_lists_every_feed_action_and_its_dark_and_light_refs(
    gymbro_repo: Path,
):
    from src.designloop.brief import build_brief

    brief = build_brief(gymbro_repo, "feed", CHANGE)

    text = brief.text
    for feature in FEED_FEATURES:
        assert feature in text, feature
    for action in FEED_ACTIONS:
        assert action in text, action
    for api in FEED_API:
        assert api in text, api
    # Other screens' features stay out of the must-keep list.
    for foreign in ("join_leave", "I'm going", "bug_report", "Report a bug"):
        assert foreign not in text, foreign

    refs = {Path(p).resolve() for p in brief.refs}
    expected = {
        ref(gymbro_repo, "feed", state, theme).resolve()
        for state in REFS["feed"]
        for theme in ("dark", "light")
    }
    assert refs == expected

    # The change request, the passport, the shared tokens and the rules.
    assert CHANGE in text
    assert "FEED_PASSPORT_MARKER" in text
    assert "EVENT_PASSPORT_MARKER" not in text
    assert "Touch targets 44" in text
    assert "UI_GUIDE_MARKER" in text


def test_brief_has_a_value_and_space_paragraph_and_states_the_problem(
    gymbro_repo: Path,
):
    """Owner 27.10 (designer audit): the brief states the problem, never
    prescribes variants, and always carries a "value and space" paragraph
    (who needs the feature, how often, how much screen it deserves)."""
    from src.designloop.brief import build_brief

    text = build_brief(gymbro_repo, "profile_tab", CHANGE).text

    assert "value and space" in text.lower()
    assert CHANGE in text
    for feature in ("own_profile", "goals", "bug_report", "Achieved"):
        assert feature in text, feature


def test_unknown_screen_is_an_error_not_an_empty_brief(gymbro_repo: Path):
    import pytest

    from src.designloop.brief import build_brief

    with pytest.raises((KeyError, ValueError)):
        build_brief(gymbro_repo, "no_such_screen", CHANGE)
