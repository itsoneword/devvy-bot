"""Fixtures for the design loop tests (dv-9133)."""

from pathlib import Path

import pytest

from tests.unit.test_designloop.helpers import make_app_repo


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    return make_app_repo(tmp_path)
