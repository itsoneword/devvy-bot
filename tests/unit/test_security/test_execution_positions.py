"""dv-3d18: a risky verb asks only where a shell runs it.

Heredoc bodies and quoted strings are data unless a shell executes them
(``src/security/destructive.py::_executed_texts``). The protected parity rows
live in ``test_policy_parity.py`` / ``test_literal_masking.py``; these are the
extra positions and shapes found while building it.
"""

from pathlib import Path

import pytest

from src.security.destructive import _segments, classify
from src.storage import board_policy

CWD = Path("/home/user/project")


def _ask(command: str):
    return classify("Bash", {"command": command}, CWD)


@pytest.mark.parametrize(
    "command",
    [
        # stdin of a shell, or of `source`
        "source /dev/stdin <<EOF\nkill 1\nEOF",
        ". /dev/stdin <<EOF\nkill 1\nEOF",
        "bash -s <<'X'\nsystemctl restart nginx\nX",
        "bash <<< 'kill 1'",
        "echo kill 1 | bash",
        "printf 'kill 1' | sh -s",
        # a process substitution a shell runs as its script
        "bash <(echo 'kill 1')",
        "source <(echo 'pkill x')",
        # commands that run their argument through a shell
        "trap 'kill 1' EXIT",
        "eval 'kill 1'",
        "watch -n 5 'pkill x'",
        "ssh -p 22 h 'kill 1'",
        "find . -exec kill {} \\;",
        "xargs -I {} kill {} < pids",
        # compound-command keywords before the command word
        "if kill -0 1; then echo; fi",
        "while true; do pkill x; done",
        # $(...) escaped inside a nested double-quoted script
        'bash -c "echo \\"\\$(kill 1)\\""',
        # catastrophic rm inside interpreter code (the narrow net)
        "python3 -c \"import os; os.system('rm -rf ~')\"",
        "python3 - <<'PY'\nimport os\nos.system('rm -rf /etc/nginx')\nPY",
        # a redirect the shell performs, heredoc or not
        "cat <<EOF > /etc/x\nhi\nEOF",
        "bash -c 'echo x > /etc/hosts'",
    ],
)
def test_executed_positions_ask(command: str) -> None:
    assert _ask(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        # dv-4b6f: a sed replacement that quotes a docker command
        "sed -i 's/`docker rm -f` lacks -v/x/' docs/a.md",
        # dv-4b6f: one rule no longer spans `|` and `;`
        "docker volume ls -q | sort > /tmp/a; echo prune",
        "git commit -m \"$(cat <<'EOF'\nfix: kill stale lock, docker rm -f c\nEOF\n)\"",
        "python3 - <<'PY'\nopen('x').write('echo hi > /etc/hosts')\nPY",
        "cat > tests/t.py <<'EOF'\nimport os\nos.kill(1, 9)\nkill 1\nEOF",
        "cat <<'EOF' | python3 -\nkill = 1\nEOF",
        # a heredoc read by a script file, not by the shell itself
        "bash scripts/x.sh <<EOF\nkill 1\nEOF",
        "diff <(sort a) <(sort b)",
        "echo 'rm -rf build'",
        "echo $((1<<3))",
        # owner answer 1.1 (2026-10-31): interpreter code is not gated, only
        # the narrow rm net above
        "python3 -c \"import os; os.system('kill 1')\"",
        "grep -rn 'kill ' src",
    ],
)
def test_data_does_not_ask(command: str) -> None:
    assert _ask(command) is None


def test_heredoc_body_stays_on_the_segment_that_reads_it() -> None:
    segs = _segments("cat <<EOF | bash\nkill 1\nEOF\necho done")
    assert [s.text for s in segs] == ["cat <<EOF", "bash", "echo done"]
    assert segs[0].heredocs == ["kill 1"]
    assert segs[1].piped and not segs[2].piped


def test_or_list_is_not_a_pipe() -> None:
    segs = _segments("false || bash")
    assert [s.text for s in segs] == ["false", "bash"] and not segs[1].piped


def _flag(tmp_path: Path, task_id: str) -> None:
    tasks = tmp_path / ".deviz" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\ntitle: t\nstatus: review\n"
        "pump_privileged: config/profiles.yaml\n---\n\nbody\n"
    )


def test_merge_subcommand_is_matched_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_policy, "TASK_DIRS", [])
    _flag(tmp_path, "dv-ab12")
    reason = board_policy.privileged_merge_reason
    for cmd in (
        "git merge-base --is-ancestor abc pump/dv-ab12",
        "git log --grep merge pump/dv-ab12",
        "git branch-x pump/dv-ab12",
    ):
        assert reason(cmd, tmp_path) is None, cmd
    for cmd in (
        "git -c core.x=1 merge pump/dv-ab12",
        "git --no-pager merge pump/dv-ab12",
        "git -C /repo merge --no-ff pump/dv-ab12",
    ):
        assert reason(cmd, tmp_path), cmd


def test_pump_ref_must_be_in_the_merging_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_policy, "TASK_DIRS", [])
    _flag(tmp_path, "dv-ab12")
    for cmd in (
        "git merge main; git log pump/dv-ab12",
        "python3 - <<'PY'\nprint('git merge pump/dv-ab12')\nPY",
    ):
        assert classify("Bash", {"command": cmd}, tmp_path) is None, cmd
    assert classify("Bash", {"command": "cd x && git merge pump/dv-ab12"}, tmp_path)
