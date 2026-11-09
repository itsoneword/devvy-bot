"""dv-32fe: one reader of config/taskpump/boards.json (src/utils/boards.py)."""

import json
from pathlib import Path

import pytest

from src.llm_analytics.attribution import load_boards
from src.storage.memory_repository import ResultPointer, render_pointer_line
from src.utils.boards import agent_repos, board_of_profile, read_board_entries


def _write(tmp_path: Path, boards: object) -> Path:
    path = tmp_path / "boards.json"
    path.write_text(json.dumps({"boards": boards}))
    return path


def test_agent_repos_maps_profiles_home_first(tmp_path):
    path = _write(
        tmp_path,
        [
            {"name": "app", "repo": "/srv/app", "profile": "app"},
            {"name": "deviz", "repo": "/srv/deviz", "profile": "devops"},
            {"name": "noprof", "repo": "/srv/noprof"},
            {"name": "rel", "repo": "relative/path", "profile": "x"},
            "junk",
            {"repo": "/srv/nameless", "profile": "y"},
        ],
    )
    repos = agent_repos(path, home_repo=Path("/srv/home"))
    assert repos == {
        "devops": (Path("/srv/home"), Path("/srv/deviz"), Path("/srv/noprof")),
        "app": (Path("/srv/app"),),
    }


def test_agent_repos_missing_or_broken_file_is_home_only(tmp_path):
    home = {"devops": (Path("/srv/home"),)}
    assert agent_repos(tmp_path / "nope.json", home_repo=Path("/srv/home")) == home
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    assert agent_repos(bad, home_repo=Path("/srv/home")) == home


def test_read_board_entries_raises_for_callers_policy(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(ValueError):
        read_board_entries(bad)
    with pytest.raises(OSError):
        read_board_entries(tmp_path / "nope.json")


def test_attribution_names_unchanged(tmp_path):
    path = _write(tmp_path, [{"name": "app"}, {"name": 3}, "junk", {"name": "site"}])
    assert load_boards(path) == frozenset({"app", "site"})
    assert load_boards(None) == frozenset()


def test_flagged_pointer_renders_flag_not_arrow():
    p = ResultPointer(
        id=1,
        recorded_at=None,
        label=None,
        source_ref="missing:docs/DECISIONS.md",
        summary="dv-1ab3 header",
    )
    line = render_pointer_line(p)
    assert "⚑ not yet in docs/DECISIONS.md" in line and "→" not in line


def test_board_of_profile_home_then_entries(tmp_path):
    path = tmp_path / "boards.json"
    path.write_text(
        json.dumps(
            {
                "pump2_home": {"profile": "devops"},
                "boards": [
                    {"name": "app", "repo": "/srv/app", "profile": "app"},
                    {"name": "deviz", "repo": "/srv/deviz"},
                    {"name": "second", "repo": "/srv/s", "profile": "app"},
                    "junk",
                    {"repo": "/srv/nameless", "profile": "y"},
                ],
            }
        )
    )
    assert board_of_profile("devops", path) == "devvy"  # home wins over deviz
    assert board_of_profile("app", path) == "app"  # first entry
    assert board_of_profile("y", path) is None  # nameless entry
    assert board_of_profile("chef", path) is None


def test_board_of_profile_home_without_pump2_home_key(tmp_path):
    """Like boards.example.json: no ``pump2_home``, an entry with no profile.
    The home profile still gets the home board, never that entry (review)."""
    path = tmp_path / "boards.json"
    path.write_text(json.dumps({"boards": [{"name": "other", "repo": "/srv/o"}]}))
    assert board_of_profile("devops", path) == "devvy"
    assert board_of_profile("app", path) is None


def test_board_of_profile_missing_or_broken_file(tmp_path):
    # The home board needs no file (old taskpump load_boards put it first).
    assert board_of_profile("devops", tmp_path / "missing.json") == "devvy"
    assert board_of_profile("app", tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("[1]")
    assert board_of_profile("devops", bad) == "devvy"
    assert board_of_profile("app", bad) is None
    bad.write_text("{nope")
    assert board_of_profile("app", bad) is None
