"""dv-9d5f follow-ups not covered by test_prompt_surfaces.py (protected)."""

from __future__ import annotations

from src.claude.agent_tools import AGENT_TOOL_SPECS


def _description(name: str) -> str:
    spec = next(s for s in AGENT_TOOL_SPECS if s.name == name)
    return str(spec.description).lower()


def test_task_add_description_drops_single_board_claim() -> None:
    # app and site have their own boards (dv-57ba); task_add sees only devvy.
    desc = _description("task_add")
    assert "one board, shared by every agent" not in desc
    assert "devvy" in desc
    assert "filed, not started" in desc
