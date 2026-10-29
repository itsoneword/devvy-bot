"""Nothing starts a model on its own: every call goes through the model
router (dv-f59b / dv-ef90). A new file matching one of these patterns is a
second way of calling a model and fails here."""

import re
from pathlib import Path
from typing import Dict, Set

REPO = Path(__file__).resolve().parents[2]

# pattern -> files allowed to contain it (the router's adapters, plus tools
# that test the harness itself).
ALLOWED: Dict[str, Set[str]] = {
    r"ClaudeSDKClient\(": {
        "src/claude/sdk_integration.py",  # the Claude turn adapter
        "src/model_router/claude_plain.py",  # the Claude plain-call adapter
        "src/model_router/job.py",  # the background-job turn adapter
        "scripts/probe_isolation.py",  # sandbox probe, tests the harness
    },
    r"create_subprocess_exec\(\s*[\"']claude[\"']": set(),
    r"\bclaude\b[^\n]*\s-p\b": set(),
    r"bypassPermissions": {
        "src/claude/agent_tools.py",  # docstring on bgjob
        "src/model_router/job.py",  # the hook is the gate for a job
    },
    r"[\"']exec[\"'],\s*\n?\s*[\"']--json[\"']": {
        "src/agent_backends/codex_cli.py",  # the Codex turn adapter
        "src/model_router/codex_plain.py",  # the Codex plain-call adapter
        "src/model_router/job_codex.py",  # the Codex background-job adapter
    },
    r"openrouter\.ai/api/v1/chat": {"src/model_router/openrouter_plain.py"},
    r"api\.typesafe\.ai": {"src/model_router/typesafe_decide.py"},
}

# Not moved yet (dv-ef90, waits for the owner's sign-off on how background
# jobs run). This set only shrinks.
PENDING: Set[str] = set()

_SKIP_DIRS = {"__pycache__", "node_modules", "reports", ".venv"}


def _sources():
    for root in ("src", "scripts", "agents"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or _SKIP_DIRS & set(path.parts):
                continue
            if path.suffix in {".py", ".sh"} or path.suffix == "":
                yield path


def test_no_model_call_outside_the_router():
    stray = []
    for path in _sources():
        rel = str(path.relative_to(REPO))
        if rel in PENDING:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, allowed in ALLOWED.items():
            if rel not in allowed and re.search(pattern, text):
                stray.append(f"{rel}: {pattern}")
    assert stray == []


def test_pending_files_still_exist():
    """Drop a file from PENDING when it moves, so the guard covers it."""
    for rel in PENDING:
        assert (REPO / rel).exists(), rel
