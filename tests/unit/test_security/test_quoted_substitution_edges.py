"""Shell escapes in quoted data must not become execution positions."""

from pathlib import Path

import pytest

from src.security.destructive import classify


@pytest.mark.parametrize(
    "command",
    [
        'echo "\\$(kill 1)"',
        'printf "\\`kill 1\\`"',
        "bash -c 'echo \"\\$(kill 1)\"'",
    ],
)
def test_escaped_substitution_is_data(command: str) -> None:
    assert classify("Bash", {"command": command}, Path("/tmp")) is None


def test_substitution_passed_to_shell_script_is_executed() -> None:
    command = 'bash -c "echo \\"\\$(kill 1)\\""'
    assert classify("Bash", {"command": command}, Path("/tmp")) is not None
