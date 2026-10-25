"""Command rules skip literal text unless it is a command line (dv-af70)."""

from pathlib import Path

import pytest

from src.security.destructive import _mask_literals, classify

CWD = Path("/home/user/project")


def _ask(command: str):
    return classify("Bash", {"command": command}, CWD)


def test_mask_blanks_quotes_and_returns_bodies() -> None:
    masked, bodies = _mask_literals('echo \'kill 1\' "a \\"b\\" c"')
    assert "kill" not in masked and "a" not in masked.split("echo", 1)[1]
    assert len(masked) == len('echo \'kill 1\' "a \\"b\\" c"')
    assert bodies == ["kill 1", 'a "b" c']


def test_mask_heredoc_keeps_lines_and_terminator() -> None:
    command = "python3 - <<'EOF'\nprint('kill')\nEOF\necho done"
    masked, bodies = _mask_literals(command)
    assert masked.count("\n") == command.count("\n")
    assert "print" not in masked and masked.endswith("EOF\necho done")
    assert bodies == ["print('kill')"]


def test_unterminated_quote_masks_nothing() -> None:
    assert _mask_literals("echo 'kill 1") == ("echo 'kill 1", [])
    assert _ask("echo 'kill 1") is not None


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix: kill stale lock"',
        "grep -rn 'no kill here' src",
        "python3 -c \"print('a pkill word')\"",
        "echo $(ls /x 2>/dev/null)/tools",
    ],
)
def test_literal_text_does_not_ask(command: str) -> None:
    assert _ask(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/kill 5",
        "bash -c '/bin/kill 5'",
        "python3 -c \"import os; os.system('kill 1')\"",
        'echo "`kill 1`"',
        "bash <<EOF\nkill 1\nEOF",
        "cat <<EOF | bash\nsystemctl restart nginx\nEOF",
        "sudo bash -c 'git push -f origin main'",
        "bash -c ':(){ :|:& };:'",
        "bash -c 'echo x > /etc/hosts'",
    ],
)
def test_commands_inside_literals_still_ask(command: str) -> None:
    assert _ask(command) is not None


def test_literal_starting_with_a_command_word_still_asks() -> None:
    # Fail-safe by design: a literal is checked as a command line.
    assert _ask("grep -rn 'kill ' src") is not None
