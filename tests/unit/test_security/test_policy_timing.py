"""dv-d760: policy.decide stays fast on real commands (timing regression).

The 6-30 s "Slow control call hook:PreToolUse" lines were the owner tapping a
confirm card, not the policy. This guards the policy side: on a corpus of real
devops Bash commands (from ``tool_calls``, 23-25 Oct, see
``docs/research/dv-d760/analysis.md``) ``decide()`` p95 stays under 50 ms
(measured 3.3 ms). Bounds are generous for CI noise.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import List

from src.security import policy

CWD = Path("/home/testuser/Devvybot/devvy_bot")

CORPUS: List[str] = [
    "uptime; free -h; df -h /",
    "vmstat 1 3; ss -ltnp | head -20",
    "curl -s -o /dev/null -w '%{http_code}' https://example.com",
    'for p in a b; do curl -s -o "$TMPDIR/x" https://example.com/$p; done',
    "time curl -s https://example.com > x.out; rm -f x.out",
    "cd ~/gymbro-prod && git log --oneline -5",
    "journalctl -u claude-bot --since '-10m' --no-pager | tail -50",
    "journalctl -u claude-bot --since today | grep 'Slow control call' | wc -l",
    "systemctl status claude-bot --no-pager",
    "git status --short",
    "git diff --stat HEAD~1",
    "git log --oneline -20",
    "git show HEAD --stat",
    "ls -la src/security",
    "cat src/security/policy.py | head -80",
    "grep -rn 'def decide' src/",
    "grep -rn credentials src/ | head",
    "rg -n 'SLOW_CALL_LOG_SECONDS' src tests",
    "find . -name '*.py' -newer pyproject.toml | head",
    "wc -l src/claude/*.py",
    "poetry run pytest tests/unit/test_turn_activity.py -q",
    "poetry run mypy src/claude/turn_activity.py",
    "make test-core",
    "python3 -c \"import os; print(os.environ['HOME'])\"",
    "python3 -c \"import sqlite3; c=sqlite3.connect('file:data/db/bot.db?mode=ro',"
    " uri=True); print(c.execute('select count(*) from tool_calls').fetchone())\"",
    "sqlite3 'file:data/db/bot.db?mode=ro' 'select count(*) from model_calls'",
    "scripts/taskpump status",
    "scripts/taskpump pending",
    "scripts/trace --last 5",
    "ps aux --sort=-%cpu | head -15",
    "top -bn1 | head -20",
    "docker ps --format '{{.Names}} {{.Status}}'",
    "du -sh data/* 2>/dev/null | sort -h | tail",
    "echo $(ls x 2>/dev/null)",
    "head -n 40 docs/FINDINGS.md",
    "sed -n 60,120p src/claude/confirmation.py",
    "mkdir -p /tmp/dv-d760 && cp src/security/policy.py /tmp/dv-d760/",
    "rm -rf /tmp/dv-d760",
    "rm notes.txt",
    "kill 12345",
    "git push origin main",
    "cat ~/.ssh/id_rsa",
]


def _turn() -> policy.TurnPolicy:
    return policy.TurnPolicy(working_directory=CWD, full_access=True)


def test_corpus_is_about_forty_real_commands() -> None:
    assert len(CORPUS) >= 40
    assert len(set(CORPUS)) == len(CORPUS)


def test_decide_p95_under_50ms_on_real_corpus() -> None:
    turn = _turn()
    # Warm-up: the first call pays the lazy import of src.claude.monitor (~1 s).
    policy.decide("Bash", {"command": "ls"}, turn)

    samples: List[float] = []
    for _ in range(3):
        for command in CORPUS:
            t0 = time.perf_counter()
            policy.decide("Bash", {"command": command}, turn)
            samples.append(time.perf_counter() - t0)

    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    worst = samples[-1]
    median_ms = statistics.median(samples) * 1000
    assert p95 < 0.050, f"p95 {p95 * 1000:.1f} ms (median {median_ms:.1f} ms)"
    assert worst < 0.200, f"max {worst * 1000:.1f} ms"
