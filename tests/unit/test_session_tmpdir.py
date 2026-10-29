"""The suite's temp files stay in a per-run dir (tests/conftest.py)."""

import tempfile
from pathlib import Path


def test_mktemp_lands_in_the_runs_private_dir() -> None:
    path = Path(tempfile.mktemp(suffix=".db"))
    assert path.parent.name.startswith("devvy-pytest-")


def test_tmp_path_is_inside_the_runs_private_dir(tmp_path: Path) -> None:
    assert any(p.name.startswith("devvy-pytest-") for p in tmp_path.parents)
