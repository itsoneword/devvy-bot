"""The background-job turn (dv-ef90): same policy as chat, asks through the
bot, denied when the bot cannot answer."""

from pathlib import Path
from unittest.mock import AsyncMock


from src.model_router.job import (
    ASK_TIMEOUT_SECONDS,
    BotLink,
    JobSpec,
    job_options,
    policy_hook,
)
from src.security import policy

TURN = policy.TurnPolicy(
    working_directory=Path("/home/u/work"), full_access=True, unrestricted=True
)


def _hook(link: BotLink):
    return policy_hook(TURN, link, source="pump-dv-1")


async def test_denied_path_is_denied_without_asking_the_bot() -> None:
    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock()  # type: ignore[method-assign]

    out = await _hook(link)(
        {"tool_name": "Bash", "tool_input": {"command": "cat .env"}}, "t1", None
    )

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    link.ask.assert_not_awaited()


async def test_ask_rule_goes_to_the_bot_and_follows_its_answer() -> None:
    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock(return_value=True)  # type: ignore[method-assign]
    call = {"tool_name": "Bash", "tool_input": {"command": "rm /home/u/work/a.txt"}}

    assert await _hook(link)(call, "t1", None) == {}
    reason, preview, source = link.ask.await_args.args
    assert "delet" in reason.lower() and "rm" in preview and source == "pump-dv-1"

    link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
    out = await _hook(link)(call, "t2", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Denied by owner" in out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_exchange_writes_ask_on_a_sandboxed_job_too() -> None:
    """dv-8664: the exchange rule lives in decide(), so jobs get it for free,
    for the tool and for the script it wraps."""
    sandboxed = policy.TurnPolicy(working_directory=Path("/home/u/work"))
    for full_access, call in (
        (False, {"tool_name": "mcp__agent__exchange_transfer", "tool_input": {}}),
        (False, {"tool_name": "mcp__x__exchange_report", "tool_input": {}}),
        (
            True,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "node ~/tools/fetchmexc/transfer.js"},
            },
        ),
    ):
        link = BotLink("http://127.0.0.1:1", "s")
        link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
        hook = policy_hook(TURN if full_access else sandboxed, link, source="job")
        out = await hook(call, "t1", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        link.ask.assert_awaited_once()


async def test_plain_call_is_allowed() -> None:
    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock()  # type: ignore[method-assign]
    out = await _hook(link)(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, "t1", None
    )
    assert out == {} and not link.ask.await_count


async def test_unreachable_bot_means_deny() -> None:
    # Nothing listens on port 1; the ask must fail closed, not raise.
    link = BotLink("http://127.0.0.1:1", "s")
    assert await link.ask("reason", "preview", "job") is False
    assert await link.record({"purpose": "background_job"}) is False


async def test_missing_secret_means_deny() -> None:
    link = BotLink("http://127.0.0.1:1", None)
    assert await link.ask("reason", "preview", "job") is False


def test_job_options_keep_the_print_mode_defaults_and_add_the_gate() -> None:
    spec = JobSpec(
        purpose="background_job",
        name="pump-dv-1",
        prompt="do it",
        model="opus",
        working_directory=Path("/home/u/work"),
        full_access=True,
        unrestricted=True,
        profile="devops",
        cli_path="/bin/claude",
    )
    options = job_options(spec, BotLink("http://127.0.0.1:1", "s"))

    assert options.model == "opus" and options.cwd == "/home/u/work"
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == ["user", "project"]
    assert options.cli_path == "/bin/claude"
    assert options.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    assert options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    (matcher,) = options.hooks["PreToolUse"]
    assert matcher.timeout > ASK_TIMEOUT_SECONDS
    assert len(matcher.hooks) == 1


def test_ask_timeout_is_one_hour() -> None:
    assert ASK_TIMEOUT_SECONDS == 3600
