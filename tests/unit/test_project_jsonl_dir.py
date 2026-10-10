"""`_project_jsonl_dir` must match the CLI's own project-directory naming.

The CLI sanitizes a cwd by replacing every non-alphanumeric character with
``-``. Replacing only ``/`` pointed the helper at a directory that never
exists for any cwd with ``_`` or ``.`` in it, which made the facade's
transcript-exists check always fail and delete a healthy session mapping on
the first process error (2026-09-17, devops profile at .../devvy_bot).
"""

from pathlib import Path

import pytest

from src.claude.sdk_integration import _project_jsonl_dir


@pytest.mark.parametrize(
    "cwd, expected",
    [
        ("/home/x/Devvybot/devvy_bot", "-home-x-Devvybot-devvy-bot"),
        ("/home/x/coliv.prj", "-home-x-coliv-prj"),
        ("/home/x/plain", "-home-x-plain"),
    ],
)
def test_matches_cli_sanitization(cwd: str, expected: str) -> None:
    assert _project_jsonl_dir(Path(cwd)).name == expected
    assert _project_jsonl_dir(Path(cwd)).parent == Path.home() / ".claude" / "projects"
