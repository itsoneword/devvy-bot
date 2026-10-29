"""dv-d2a3 phase 2: Overview source filter beyond the approved scenarios.

Reuses the fixture world, server and node harness of test_overview_ui.
"""

from pathlib import Path

from tests.unit.test_llm_analytics.test_overview_ui import (  # noqa: F401
    _card,
    _last,
    _no_render_errors,
    _run_ui,
    _snap,
    _text,
    server,
    ui_world,
)


def test_agent_source_narrows_quality_card_to_that_profile(
    server: str, tmp_path: Path  # noqa: F811
) -> None:
    out = _run_ui(
        server,
        tmp_path,
        [
            {"op": "select_source", "match": "agent/coach"},
            {"op": "snapshot", "label": "coach"},
        ],
    )
    _no_render_errors(out)
    quality = _last(_snap(out, "coach")["requests"], "api/quality")
    assert quality["params"].get("profile") == "coach"
    assert "source" not in quality["params"]


def test_non_agent_source_skips_quality_and_keeps_source_card(
    server: str, tmp_path: Path  # noqa: F811
) -> None:
    out = _run_ui(
        server,
        tmp_path,
        [
            {"op": "snapshot", "label": "all"},
            {"op": "select_source", "match": "pump/gymbro"},
            {"op": "snapshot", "label": "pump"},
        ],
    )
    _no_render_errors(out)
    before = _snap(out, "all")
    after = _snap(out, "pump")
    new = after["requests"][len(before["requests"]) :]
    assert not [r for r in new if r["path"] == "api/quality"]
    # The tree follows the filter: only the Pump group is left.
    card = _card(after["tree"], "source")
    assert card is not None
    text = _text(card)
    assert "Pump" in text
    assert "coach" not in text and "devops" not in text, text
