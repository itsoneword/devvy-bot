"""docs/pump.md "How it actually works" and "Writing a card" (dv-ebcb)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "pump.md").read_text()
CAPS = ROOT / "data" / "shared-capabilities.md"


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", DOC, re.S | re.M)
    assert m, f"section {title!r} missing"
    return m.group(1)


def test_C1_how_it_works_tick_and_fresh_job() -> None:
    s = _section("How it actually works")
    for needle in (
        "pump2.timer",
        "scripts/pump2 tick",
        "bgjob run",
        "--route pump_",
        "--setting-sources",
        "project",
        "--cwd",
        "--profile",
        "bgjob_launch_stage",
        "plan.md",
        "tests.json",
        "review.md",
        "no memory",
    ):
        assert needle in s, needle
    assert len(re.findall(r"\w+\.py:\d+", s)) >= 10


def test_C2_agent_visibility_answers() -> None:
    s = _section("How it actually works")
    low = s.lower()
    assert "claude.md" in low and ".claude/skills" in low
    assert "persona" in low and "subagent" in low
    assert "**yes**" in s and "**no**" in s


def test_C3_stage_table() -> None:
    s = _section("How it actually works")
    for needle in (
        "analyze",
        "tests",
        "build",
        "review",
        "custom",
        "merge",
        "mergefix",
        "pump_analyze",
        "pump_testdesign",
        "pump_implement",
        "pump_review",
        "pump_mergefix",
        "STAGE_ROUTE",
        "config/models.yaml",
        "routes.py",
        "no model",
    ):
        assert needle in s, needle


def test_C4_writing_a_card() -> None:
    s = _section("Writing a card")
    for needle in (
        "title",
        "labels",
        "deps",
        "start_at",
        "touches",
        "size",
        "kind",
        "pump2:",
        "designloop",
        "stage_labels",
        "Minimum fields",
        "Bad",
        "Good",
    ):
        assert needle in s, needle
    assert "state.py" in s and "warn_labels" in s


def test_C5_shared_capabilities_pointer_and_budget() -> None:
    text = CAPS.read_text()
    assert "docs/pump.md" in text and "Writing a card" in text
    assert len(text.encode()) <= 10080


def test_C6_cited_lines_exist() -> None:
    s = _section("How it actually works") + _section("Writing a card")
    for path, line in re.findall(
        r"((?:src|scripts|deploy|config)/[\w/.\-]+?):(\d+)", s
    ):
        p = ROOT / path
        if p.is_file():
            n = len(p.read_text().splitlines())
            assert int(line) <= n, f"{path}:{line} beyond EOF ({n})"
