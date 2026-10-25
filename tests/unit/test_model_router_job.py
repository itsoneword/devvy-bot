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


async def test_job_router_records_through_the_bot() -> None:
    """dv-e690: a read-only bot.db must not lose router calls from jobs."""
    from src.model_router.job import RemoteModelCallRecorder
    from src.storage.model_calls import ModelCall

    link = BotLink("http://127.0.0.1:1", "s")
    link.record = AsyncMock(return_value=True)  # type: ignore[method-assign]
    call = ModelCall(
        purpose="screen",
        kind="decide",
        harness="typesafe",
        harness_version=None,
        provider="typesafe",
        model="jev-latest",
        attended=False,
        profile=None,
        user_id=None,
        session_id=None,
        input_tokens=5,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.1,
        duration_ms=3,
        outcome="ok",
        error=None,
    )
    await RemoteModelCallRecorder(link).record(call)
    row = link.record.call_args.args[0]
    assert row["purpose"] == "screen" and row["harness"] == "typesafe"
    assert row["input_tokens"] == 5


async def test_run_job_sends_its_trace_and_links_it(monkeypatch) -> None:
    """dv-2260: a job's tool calls go to the bot as they happen and the
    model_calls row carries the trace id; an interrupted job keeps them."""
    import io

    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    from src.model_router import job as job_mod

    stream = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            model="m",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="a b")]),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="js",
            total_cost_usd=0.1,
            usage={},
            result="done",
        ),
    ]

    class FakeClient:
        def __init__(self, *_: object) -> None:
            pass

        async def connect(self) -> None: ...
        async def query(self, _: str) -> None: ...
        async def disconnect(self) -> None: ...

        async def receive_response(self):
            for m in stream:
                yield m

    monkeypatch.setattr(job_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(job_mod, "job_options", lambda *_: None)
    link = BotLink("http://127.0.0.1:1", "s")
    posted = []
    link.post = lambda path, body, timeout: posted.append((path, body)) or {}  # type: ignore[method-assign]
    spec = job_mod.JobSpec(
        purpose="background_job",
        name="j",
        prompt="p",
        model="sonnet",
        working_directory=Path("/tmp"),
        full_access=False,
        unrestricted=False,
    )
    code = await job_mod.run_job(spec, link, out=io.StringIO())
    assert code == 0
    tool_posts = [b for p, b in posted if p == "/internal/tool_calls"]
    (call_post,) = [b for p, b in posted if p == "/internal/model_calls"]
    assert tool_posts[-1]["rows"][0]["tool_name"] == "Bash"
    assert tool_posts[-1]["rows"][0]["ok"] == 1
    assert call_post["trace_id"] == tool_posts[-1]["trace_id"]
