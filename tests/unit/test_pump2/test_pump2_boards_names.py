"""src/utils/boards.pump2_board_names (dv-d474)."""

import json
from pathlib import Path

from src.utils.boards import pump2_board_names


def test_home_and_flagged_boards(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(
        json.dumps(
            {
                "pump2_home": {"profile": "devops"},
                "boards": [
                    {"name": "app", "repo": "/x", "pump2": True},
                    {"name": "site", "repo": "/y"},
                ],
            }
        )
    )
    assert pump2_board_names(p) == {"devvy", "app"}


def test_no_home_key_and_broken_file(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"boards": [{"name": "site", "repo": "/y"}]}))
    assert pump2_board_names(p) == set()
    p.write_text("{nope")
    assert pump2_board_names(p) == set()
    assert pump2_board_names(tmp_path / "missing.json") == set()
