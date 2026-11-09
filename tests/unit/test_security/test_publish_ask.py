"""dv-b05e: every agent path to the public repo asks; the read-only ones don't.

The parity rows (test_policy_parity.py) pin the plain spellings; these pin the
wrappers and look-alikes the parsed-command rule has to get right.
"""

from pathlib import Path

import pytest

from src.security.destructive import classify

CWD = Path("/tmp")


def _reason(command: str):
    return classify("Bash", {"command": command}, CWD)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'scripts/release publish devvy'",
        "cd /opt/bot && scripts/release publish devvy --by owner",
        "/opt/bot/scripts/release publish pl",
        "python3 scripts/release_public.py publish --staging /tmp/s",
        "sh -c 'gh release upload v1.7.0 notes.md'",
        "gh release edit v1.7.0 --draft=false",
        # review 02.11: release under an interpreter runs the same push.
        "python3 scripts/release publish devvy",
        "/opt/bot/.venv/bin/python scripts/release publish devvy",
        "bash scripts/release publish devvy",
        "poetry run python scripts/release publish devvy",
    ],
)
def test_publish_paths_ask(command):
    assert _reason(command), command


@pytest.mark.parametrize(
    "command",
    [
        "scripts/release check devvy",
        "scripts/release later devvy",
        "grep -n 'release publish' docs/release.md",
        "python3 scripts/release_public.py daily",
        "gh release view v1.7.0",
        "echo release_public.py publish",
        "python3 scripts/release check devvy",
        "bash scripts/release tick",
        "python3 scripts/pump2 answer dv-1 publish",
    ],
)
def test_reads_and_mentions_do_not_ask(command):
    assert _reason(command) is None, command
