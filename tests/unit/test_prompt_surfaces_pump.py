"""dv-9d5f follow-ups not covered by test_prompt_surfaces.py (protected)."""

from __future__ import annotations

from pathlib import Path

from src.claude.agent_tools import AGENT_TOOL_SPECS

ROOT = Path(__file__).resolve().parents[2]


def _description(name: str) -> str:
    spec = next(s for s in AGENT_TOOL_SPECS if s.name == name)
    return str(spec.description).lower()


def test_task_add_description_drops_single_board_claim() -> None:
    # gymbro and pl have their own boards (dv-57ba); task_add sees only devvy.
    desc = _description("task_add")
    assert "one board, shared by every agent" not in desc
    assert "devvy" in desc
    assert "filed, not started" in desc


def test_analyze_prompt_asks_owner_only_owner_level_questions() -> None:
    # Owner 2026-10-24: low-stakes product/UI details are defaults, not questions.
    text = (ROOT / "config" / "taskpump" / "analyze.md").read_text(encoding="utf-8")
    assert "security" in text and "irreversible" in text
    assert "assumptions" in text
