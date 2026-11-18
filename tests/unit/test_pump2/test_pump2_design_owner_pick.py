"""A design run builds to the mockup the owner already chose for the screen."""

from pathlib import Path
from types import SimpleNamespace

from src.pump2.design import owner_pick


def _board(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(repo=tmp_path)


def test_saved_mockup_for_the_screen_is_found(tmp_path: Path) -> None:
    config = SimpleNamespace(references="docs/refs/README.md")
    pick = tmp_path / "docs/refs/owner-mockups/profile_tab.png"
    pick.parent.mkdir(parents=True)
    pick.write_bytes(b"png")
    assert owner_pick(_board(tmp_path), config, "profile_tab") == pick
    assert owner_pick(_board(tmp_path), config, "feed") is None


def test_no_references_or_no_screen_means_no_pick(tmp_path: Path) -> None:
    assert owner_pick(_board(tmp_path), SimpleNamespace(references=None), "x") is None
    config = SimpleNamespace(references="docs/refs/README.md")
    assert owner_pick(_board(tmp_path), config, "") is None
