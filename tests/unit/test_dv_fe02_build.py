"""dv-fe02 phase 3, build-side checks next to the protected test files.

* ``bot_claude_md_excludes``: legacy layout (no data root) still names the
  checkout's parent; root mode names the data root's parent; no duplicates.
* The release templates carry the audit's text fixes (report T1-T3).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.utils.paths import DATA_ROOT_VAR, REPO_ROOT, bot_claude_md_excludes

TEMPLATES = REPO_ROOT / "data" / "templates" / "profiles"


def test_excludes_root_mode_name_global_and_data_root_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "install" / "data"
    excludes = bot_claude_md_excludes(env={DATA_ROOT_VAR: str(data)})
    assert excludes[0] == str(tmp_path / "home" / ".claude" / "CLAUDE.md")
    assert str(tmp_path / "install" / "CLAUDE.md") in excludes
    assert str(REPO_ROOT.parent / "CLAUDE.md") in excludes
    assert len(set(excludes)) == len(excludes)


def test_excludes_legacy_layout_names_the_checkouts_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    excludes = bot_claude_md_excludes(env={}, dotenv=tmp_path / "missing.env")
    assert excludes == (
        str(tmp_path / "home" / ".claude" / "CLAUDE.md"),
        str(REPO_ROOT.parent / "CLAUDE.md"),
    )
    assert str(REPO_ROOT / "CLAUDE.md") not in excludes


def test_excludes_documented_layout_has_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data root = <checkout's parent>/data: one install-dir entry, not two."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = REPO_ROOT.parent / "data"
    excludes = bot_claude_md_excludes(env={DATA_ROOT_VAR: str(data)})
    assert excludes == (
        str(tmp_path / "home" / ".claude" / "CLAUDE.md"),
        str(REPO_ROOT.parent / "CLAUDE.md"),
    )


def test_pm_template_names_no_unmounted_task_done() -> None:
    text = (TEMPLATES / "pm" / "persona.md").read_text(encoding="utf-8")
    assert "task_done" not in text
    assert "task_comment" in text


def test_coach_template_logs_through_the_data_tools() -> None:
    text = (TEMPLATES / "coach" / "persona.md").read_text(encoding="utf-8")
    assert "log_food" in text and "never read or edit data files" in text
    assert "recovery_log.md" not in text and "via Edit" not in text


def test_example_coach_mounts_the_same_data_tools_as_the_template() -> None:
    example = yaml.safe_load(
        (REPO_ROOT / "config" / "profiles.example.yaml").read_text(encoding="utf-8")
    )
    coach = next(p for p in example["profiles"] if p["slug"] == "coach")
    template = yaml.safe_load(
        (TEMPLATES / "coach" / "profile.yaml").read_text(encoding="utf-8")
    )[0]
    assert coach["user_data_tools"] == template["user_data_tools"]
    assert coach.get("today_digest") == template.get("today_digest")


def test_backend_smoke_names_its_own_install_dir_canary() -> None:
    """The smoke plants INSTALL_CANARY in its install dir; quoting it FAILs and
    the note names the canary, not the real install-dir file."""
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / "backend_smoke.py"
    spec = importlib.util.spec_from_file_location("backend_smoke_build", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module  # type: ignore[union-attr]
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    notes: list[str] = []
    assert module.claude_md_check(module.INSTALL_CANARY, notes) == "FAIL"
    assert "canary" in notes[0]
    assert module.claude_md_check("NO-CLAUDE-MD", []) == "PASS"
