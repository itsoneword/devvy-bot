"""A partial Codex answer must not hide a later usage-limit failure."""

import json
import stat
from datetime import UTC, datetime

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.model_router import quota
from tests.unit.test_codex_cli_integration import settings  # noqa: F401 (fixture)
from tests.unit.test_model_router import _router, _rows, db  # noqa: F401 (fixture)


@pytest.mark.parametrize("answer_source", ["event", "output_file"])
@pytest.mark.parametrize("purpose,attended", [("chat", True), ("scheduled", False)])
async def test_partial_answer_then_limit_records_failed_turn(
    tmp_path, settings, db, monkeypatch, answer_source, purpose, attended
):
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(tmp_path / "data"))
    observed = (
        "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
        "visit https://chatgpt.com/codex/settings/usage to purchase more credits "
        "or try again at 10:24 PM."
    )
    events = [
        {"type": "thread.started", "thread_id": "th-partial"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial"},
        },
        {"type": "turn.failed", "error": {"message": observed}},
    ]
    script = tmp_path / "codex_partial_limit"
    lines = [
        "#!/usr/bin/env bash",
        'outfile=""',
        'args=("$@")',
        "for ((i=0; i<${#args[@]}; i++)); do",
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi',
        "done",
    ]
    lines.extend(f"printf '%s\\n' '{json.dumps(event)}'" for event in events)
    if answer_source == "output_file":
        lines.append('printf partial > "$outfile"')
    lines.append("exit 1")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(script),
        session_store=InMemoryCodexSessionStore(),
    )
    with pytest.raises(RuntimeError, match="OpenAI usage limit exhausted"):
        await _router(db).run_turn(
            integ,
            purpose=purpose,
            attended=attended,
            prompt="hi",
            working_directory=tmp_path,
            user_id=1,
            force_new=True,
        )

    (row,) = await _rows(db)
    assert (row["purpose"], row["outcome"], row["provider"]) == (
        purpose,
        "error",
        "openai",
    )
    assert "limit" in row["error"].lower()
    assert quota.active(datetime.now(UTC), provider="openai") is not None
