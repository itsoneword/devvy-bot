"""The background-job turn (dv-ef90): same policy as chat, asks through the
bot, denied when the bot cannot answer."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

from src.model_router.job import (
    ASK_TIMEOUT_SECONDS,
    BotLink,
    JobSpec,
    job_options,
    policy_hook,
    run_job,
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


# --------------------------------------------------------------------------
# dv-2260 phase 2: a job's tool tree goes to the bot with its model_calls row.
#
# Contract pinned here (docs/research/dv-2260/analysis.md, "Phase 2"):
# * ``run_job`` feeds the stream to the same ``TraceCollector`` the router
#   uses and sends its rows as ``tools`` (a JSON list) in the one
#   ``/internal/model_calls`` POST -- no new endpoint.
# * ``create_api_app(..., model_calls=ModelCallRepository(db),
#   tool_calls=ToolCallRepository(db))``: the endpoint writes the
#   ``model_calls`` row, then the tool rows with ``model_call_id`` = its id.
# * Bot down: the job's answer and exit code are unchanged, nothing raises.
# --------------------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

import pytest  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

JOB_SECRET = "job-secret"
SUBAGENT_FINAL = "Subagent report: 3 TODOs in src/."


def _spec(tmp_path: Path) -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name="pump-dv-2260-test",
        prompt="find the TODOs",
        model="sonnet",
        working_directory=tmp_path,
        full_access=True,
        unrestricted=True,
        profile="devops",
    )


def _job_result(**kw: Any) -> ResultMessage:
    base: Dict[str, Any] = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=2,
        session_id="job-sess-1",
        total_cost_usd=0.02,
        result="There are 3 TODOs.",
        usage={"input_tokens": 10, "output_tokens": 4},
    )
    base.update(kw)
    return ResultMessage(**base)


def _job_stream(result: ResultMessage | None = None) -> List[Any]:
    """Main agent: Read + Agent; the subagent runs a Grep of its own."""
    return [
        SystemMessage(
            subtype="init",
            data={"claude_code_version": "2.1.300", "model": "claude-sonnet-x"},
        ),
        AssistantMessage(
            content=[
                ToolUseBlock(id="r1", name="Read", input={"file_path": "/w/README"}),
                ToolUseBlock(
                    id="a1",
                    name="Agent",
                    input={
                        "subagent_type": "general-purpose",
                        "description": "todos",
                        "prompt": "count TODOs",
                    },
                ),
            ],
            model="sonnet",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="r1", content="# readme")]),
        AssistantMessage(
            content=[ToolUseBlock(id="g1", name="Grep", input={"pattern": "TODO"})],
            model="sonnet",
            parent_tool_use_id="a1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="g1", content="a.py\nb.py\nc.py")],
            parent_tool_use_id="a1",
        ),
        AssistantMessage(
            content=[TextBlock(text=SUBAGENT_FINAL)],
            model="sonnet",
            parent_tool_use_id="a1",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="a1",
                    content=[{"type": "text", "text": SUBAGENT_FINAL}],
                )
            ]
        ),
        AssistantMessage(
            content=[TextBlock(text="There are 3 TODOs.")], model="sonnet"
        ),
        result if result is not None else _job_result(),
    ]


class _FakeClient:
    """Stands in for ``ClaudeSDKClient`` inside ``run_job``."""

    def __init__(self, messages: List[Any]) -> None:
        self._messages = messages

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> Any:
        for m in self._messages:
            yield m

    async def disconnect(self) -> None:
        return None


def _scripted_sdk(monkeypatch: pytest.MonkeyPatch, messages: List[Any]) -> None:
    monkeypatch.setattr(
        "src.model_router.job.ClaudeSDKClient",
        lambda options: _FakeClient(messages),
    )
    # dv-b6dd: keep the orphan scan off the test runner's own cgroup.
    monkeypatch.setattr("src.model_router.job.orphans", lambda *a, **k: [])


class _AppLink(BotLink):
    """A ``BotLink`` whose HTTP goes to an in-process bot API app. The body
    is JSON-encoded exactly as ``BotLink.post`` does, so anything that would
    not survive the wire (a raw datetime) fails here too."""

    def __init__(self, app: Any) -> None:
        super().__init__("http://bot.test", JOB_SECRET)
        from fastapi.testclient import TestClient

        self._client = TestClient(app)
        self.posts: List[str] = []

    def post(self, path: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        wire = json.dumps(body)  # raises like the real link on non-JSON values
        self.posts.append(path)
        resp = self._client.post(
            path,
            content=wire,
            headers={
                "Authorization": f"Bearer {JOB_SECRET}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code >= 400:
            raise OSError(f"HTTP {resp.status_code}: {resp.text}")
        data = resp.json() if resp.content else {}
        return data if isinstance(data, dict) else {}


@pytest.fixture
async def bot_db(tmp_path: Path):
    from src.storage.database import DatabaseManager

    db = DatabaseManager(str(tmp_path / "bot.db"))
    await db.initialize()
    yield db
    await db.close()


def _bot_app(db: Any) -> Any:
    from unittest.mock import MagicMock

    from src.api.server import create_api_app
    from src.events.bus import EventBus
    from src.storage.model_calls import ModelCallRepository
    from src.storage.tool_calls import ToolCallRepository

    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = "gh"
    settings.webhook_api_secret = JOB_SECRET
    settings.api_server_port = 8080
    settings.debug = False
    return create_api_app(
        EventBus(),
        settings,
        model_calls=ModelCallRepository(db),
        tool_calls=ToolCallRepository(db),
    )


async def _rows(db: Any, table: str) -> List[Dict[str, Any]]:
    order = "seq, id" if table == "tool_calls" else "id"
    async with db.get_connection() as conn:
        cur = await conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        return [dict(r) for r in await cur.fetchall()]


async def test_agentjob_run_writes_model_call_and_its_tool_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_db: Any
) -> None:
    """agentjob run writes model_calls row plus its tool_calls rows with that
    id; the subagent's own call sits under the Agent row."""
    _scripted_sdk(monkeypatch, _job_stream())
    link = _AppLink(_bot_app(bot_db))
    out = io.StringIO()

    code = await run_job(_spec(tmp_path), link, out=out)

    assert code == 0
    assert "There are 3 TODOs." in out.getvalue()
    # One POST to the existing endpoint carries both, no second path.
    assert link.posts == ["/internal/model_calls"]

    (call,) = await _rows(bot_db, "model_calls")
    assert call["purpose"] == "background_job" and call["outcome"] == "ok"
    assert call["session_id"] == "job-sess-1"

    tools = await _rows(bot_db, "tool_calls")
    by_id = {r["tool_use_id"]: r for r in tools}
    assert set(by_id) == {"r1", "a1", "g1"}
    assert {r["model_call_id"] for r in tools} == {call["id"]}
    assert [r["tool"] for r in tools] == ["Read", "Agent", "Grep"]
    assert by_id["r1"]["parent_tool_use_id"] is None
    assert by_id["a1"]["parent_tool_use_id"] is None
    assert by_id["g1"]["parent_tool_use_id"] == "a1"
    assert all(r["status"] == "ok" for r in tools)
    assert by_id["a1"]["subagent_type"] == "general-purpose"
    assert by_id["a1"]["result_preview"] == SUBAGENT_FINAL
    assert json.loads(by_id["r1"]["input"]) == {"file_path": "/w/README"}
    assert by_id["r1"]["started_at"] is not None
    assert by_id["g1"]["result_bytes"] == len("a.py\nb.py\nc.py")
    # Job sessions are not in `sessions`; rows carry the job's session id.
    assert {r["session_id"] for r in tools} == {"job-sess-1"}


async def test_bot_down_during_job_still_succeeds_and_trace_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bot down during job: job still succeeds, trace lost, no crash."""
    _scripted_sdk(monkeypatch, _job_stream())
    link = BotLink("http://127.0.0.1:1", "s")  # nothing listens on port 1
    out = io.StringIO()

    code = await run_job(_spec(tmp_path), link, out=out)

    assert code == 0
    assert out.getvalue().strip() == "There are 3 TODOs."


async def test_bot_down_record_receives_the_tool_rows_it_could_not_deliver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bot down: the one record attempt carried the trace (so it is the
    trace that is lost, not a second call that could hang the job)."""
    _scripted_sdk(monkeypatch, _job_stream())
    link = BotLink("http://127.0.0.1:1", "s")
    sent: List[Dict[str, Any]] = []
    real_record = link.record

    async def record(row: Dict[str, Any]) -> bool:
        sent.append(row)
        return await real_record(row)

    link.record = record  # type: ignore[method-assign]

    assert await run_job(_spec(tmp_path), link, out=io.StringIO()) == 0
    (row,) = sent
    assert [t["tool_use_id"] for t in row["tools"]] == ["r1", "a1", "g1"]
    json.dumps(row)  # wire-safe


async def test_failed_job_still_sends_its_tool_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_db: Any
) -> None:
    """added: a job that ends in error keeps the trace of what it did."""
    failed = _job_result(is_error=True, subtype="error_during_execution", result="boom")
    _scripted_sdk(monkeypatch, _job_stream(result=failed))
    link = _AppLink(_bot_app(bot_db))

    code = await run_job(_spec(tmp_path), link, out=io.StringIO())

    assert code == 1
    (call,) = await _rows(bot_db, "model_calls")
    assert call["outcome"] == "error"
    tools = await _rows(bot_db, "tool_calls")
    assert [r["tool_use_id"] for r in tools] == ["r1", "a1", "g1"]
    assert {r["model_call_id"] for r in tools} == {call["id"]}


# dv-58db: the free root is bound to pump worktrees, nothing else.


def _linked_worktree(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /x/.git/worktrees/" + path.name + "\n")
    return path


def test_pump_worktree_root_only_for_linked_pump_worktrees(
    tmp_path, monkeypatch
) -> None:
    from src.model_router.job import pump_worktree_root

    base = tmp_path.resolve()
    worktrees = base / "pump-worktrees"
    monkeypatch.setenv("PUMP_WORKTREES", str(worktrees))
    wt = _linked_worktree(worktrees / "dv-ab12")
    board_wt = _linked_worktree(worktrees / "pl" / "dv-cd34")
    git_dir = worktrees / "dv-ef56"
    (git_dir / ".git").mkdir(parents=True)
    live = base / "Devvybot" / "devvy_bot"
    (live / ".git").mkdir(parents=True)
    outside = _linked_worktree(base / "elsewhere" / "dv-9999")
    (worktrees / "dv-link").symlink_to(outside, target_is_directory=True)

    assert pump_worktree_root(wt) == wt
    assert pump_worktree_root(board_wt) == board_wt
    assert pump_worktree_root(live) is None
    assert pump_worktree_root(worktrees) is None
    assert pump_worktree_root(git_dir) is None
    assert pump_worktree_root(worktrees / "dv-link") is None


async def test_job_options_carry_the_free_root_into_the_gate(tmp_path) -> None:
    root = _linked_worktree(tmp_path.resolve() / "pump-worktrees" / "dv-ab12")
    (root / "tests").mkdir()
    (root / "tests" / "x.py").write_text("x\n")
    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock(return_value=False)  # type: ignore[method-assign]
    spec = JobSpec(
        purpose="background_job",
        name="pump-dv-ab12-build",
        prompt="do it",
        model="opus",
        working_directory=root,
        full_access=True,
        unrestricted=True,
        free_root=root,
    )
    (matcher,) = job_options(spec, link).hooks["PreToolUse"]
    (hook,) = matcher.hooks
    call = {"tool_name": "Bash", "tool_input": {"command": "rm tests/x.py"}}
    assert await hook(call, "t1", None) == {}
    assert link.ask.await_count == 0


# --------------------------------------------------------------------------
# dv-b6dd: a job must not exit "OK" while its own background work still runs
#
# Contracts the build must meet:
# - SDK task events (TaskStartedMessage / terminal TaskNotificationMessage or
#   TaskUpdatedMessage) are tracked; a task still live at a ResultMessage gets
#   a follow-up ``client.query`` (the nudge) in the same session naming it;
# - ``job.orphans(proc_root=..., cgroup_root=...)`` returns ``[(pid, cmd)]``
#   for processes left in the job's cgroup outside its own process tree;
#   run_job calls ``orphans()`` (patched here) and nudges for them too;
# - at most ``BGJOB_MAX_NUDGES`` nudges (default 2, 0 = none); still live
#   after that -> exit 3, stderr names the work, model_calls outcome
#   ``incomplete``; the answer is still printed.
# --------------------------------------------------------------------------


def _result(text: str, *, is_error: bool = False, subtype: str = "success"):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="js",
        total_cost_usd=0.1,
        usage={},
        result=text,
    )


def _started(task_id: str, description: str):
    from claude_agent_sdk import TaskStartedMessage

    return TaskStartedMessage(
        subtype="task_started",
        data={"task_id": task_id, "description": description},
        task_id=task_id,
        description=description,
        uuid="u-" + task_id,
        session_id="js",
        tool_use_id="tu-" + task_id,
        task_type="local_bash",
    )


def _notified(task_id: str, status: str = "completed"):
    from claude_agent_sdk import TaskNotificationMessage

    return TaskNotificationMessage(
        subtype="task_notification",
        data={"task_id": task_id, "status": status},
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        output_file="/tmp/out",
        summary="done",
        uuid="n-" + task_id,
        session_id="js",
    )


def _multi_turn_job(monkeypatch, turns, orphan_lists=None):
    """Patch run_job onto a fake client that plays one scripted stream per
    ``query``. Returns (run coroutine factory, queries, posted rows)."""
    import io

    from src.model_router import job as job_mod

    queries: list = []
    script = list(turns)

    class FakeClient:
        def __init__(self, *_: object) -> None:
            self._turn: list = []

        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...

        async def query(self, prompt: str, *_: object, **__: object) -> None:
            queries.append(prompt)
            self._turn = script.pop(0) if script else [_result("(no more turns)")]

        async def receive_response(self):
            for m in self._turn:
                yield m

    monkeypatch.setattr(job_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(job_mod, "job_options", lambda *_: None)
    monkeypatch.delenv("BGJOB_MAX_NUDGES", raising=False)
    orphan_script = list(orphan_lists or [])

    def fake_orphans(*_: object, **__: object):
        return orphan_script.pop(0) if orphan_script else []

    # raising=False: the guard test must run on today's code, which has no
    # orphan scan yet; the real scan must never see the test runner's cgroup.
    monkeypatch.setattr(job_mod, "orphans", fake_orphans, raising=False)
    link = BotLink("http://127.0.0.1:1", "s")
    posted: list = []
    link.post = lambda path, body, timeout: posted.append((path, body)) or {}  # type: ignore[method-assign]
    spec = job_mod.JobSpec(
        purpose="background_job",
        name="j",
        prompt="do the job",
        model="sonnet",
        working_directory=Path("/tmp"),
        full_access=False,
        unrestricted=False,
    )
    out = io.StringIO()

    async def run() -> int:
        return await job_mod.run_job(spec, link, out=out)

    return run, queries, posted, out


def _call_row(posted):
    (row,) = [b for p, b in posted if p == "/internal/model_calls"]
    return row


async def test_job_without_background_work_is_one_turn_and_ok(monkeypatch) -> None:
    """Scenario 1 (guard): no task events, no orphans -> exit 0, one query,
    outcome ok, the answer printed, as today."""
    run, queries, posted, out = _multi_turn_job(monkeypatch, [[_result("all done")]])

    assert await run() == 0
    assert queries == ["do the job"]
    assert out.getvalue().strip() == "all done"
    assert _call_row(posted)["outcome"] == "ok"


async def test_live_background_task_gets_a_nudge_and_the_finished_answer_wins(
    monkeypatch,
) -> None:
    """Scenario 2: a task started and still live at the turn's end -> one
    follow-up query naming it; the agent waits, the task completes, the
    second turn's answer is the job's answer, exit 0."""
    run, queries, posted, out = _multi_turn_job(
        monkeypatch,
        [
            [_started("bzloyp6nl", "full pytest run"), _result("tests still running")],
            [_notified("bzloyp6nl"), _result("tests passed, committed")],
        ],
    )

    assert await run() == 0
    assert len(queries) == 2
    assert "bzloyp6nl" in queries[1] and "full pytest run" in queries[1]
    assert out.getvalue().strip() == "tests passed, committed"
    assert _call_row(posted)["outcome"] == "ok"


async def test_task_still_live_after_two_nudges_exits_3_incomplete(
    monkeypatch, capsys
) -> None:
    """Scenario 3: the task never finishes -> 2 nudges (3 queries), then
    exit 3; stderr and the model_calls row name the task; outcome is
    ``incomplete``; the last answer is still printed."""
    run, queries, posted, out = _multi_turn_job(
        monkeypatch,
        [
            [_started("bzloyp6nl", "full pytest run"), _result("waiting")],
            [_result("still waiting")],
            [_result("waiting for the monitor to report")],
            [_result("must not be reached")],
        ],
    )

    assert await run() == 3
    assert len(queries) == 3
    assert out.getvalue().strip() == "waiting for the monitor to report"
    err = capsys.readouterr().err
    assert "incomplete" in err and "bzloyp6nl" in err
    row = _call_row(posted)
    assert row["outcome"] == "incomplete"
    assert "bzloyp6nl" in (row["error"] or "")


async def test_nohup_orphan_in_the_job_cgroup_counts_as_live_work(
    monkeypatch, capsys
) -> None:
    """Scenario 4: no SDK task at all, but a `nohup … &` process is left in
    the job's cgroup -> nudged with its pid and command, and after the
    nudges, with the process still there, exit 3."""
    orphan = [(4242, "python -m pytest -q tests")]
    run, queries, posted, _ = _multi_turn_job(
        monkeypatch,
        [[_result("pytest started in background")], [_result("x")], [_result("y")]],
        orphan_lists=[orphan, orphan, orphan, orphan],
    )

    assert await run() == 3
    assert len(queries) == 3
    assert "4242" in queries[1] and "pytest" in queries[1]
    assert "4242" in capsys.readouterr().err
    assert _call_row(posted)["outcome"] == "incomplete"


async def test_nohup_orphan_that_ends_after_the_nudge_gives_ok(monkeypatch) -> None:
    """Scenario 4, the good path: the orphan is gone after the agent waited
    -> exit 0 with the second answer."""
    orphan = [(4242, "python -m pytest -q tests")]
    run, queries, _, out = _multi_turn_job(
        monkeypatch,
        [[_result("started")], [_result("pytest finished: 10 passed")]],
        orphan_lists=[orphan, []],
    )

    assert await run() == 0
    assert len(queries) == 2
    assert out.getvalue().strip() == "pytest finished: 10 passed"


async def test_max_nudges_zero_marks_incomplete_without_an_extra_turn(
    monkeypatch,
) -> None:
    """added: BGJOB_MAX_NUDGES=0 -> no follow-up query, live task -> exit 3."""
    run, queries, posted, _ = _multi_turn_job(
        monkeypatch,
        [[_started("t1", "sleep 40"), _result("done")], [_result("not reached")]],
    )
    monkeypatch.setenv("BGJOB_MAX_NUDGES", "0")

    assert await run() == 3
    assert queries == ["do the job"]
    assert _call_row(posted)["outcome"] == "incomplete"


async def test_error_in_a_nudge_turn_is_a_failure_not_the_earlier_ok(
    monkeypatch,
) -> None:
    """added (architect note 5): the nudge turn ends in an error result ->
    exit 1, outcome error; the first turn's success must not leak through."""
    run, queries, posted, _ = _multi_turn_job(
        monkeypatch,
        [
            [_started("t1", "full pytest run"), _result("waiting")],
            [
                _result(
                    "API overloaded", is_error=True, subtype="error_during_execution"
                )
            ],
        ],
    )

    assert await run() == 1
    assert len(queries) == 2
    assert _call_row(posted)["outcome"] == "error"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_proc(proc: Path, pid: int, ppid: int, argv: list) -> None:
    d = proc / str(pid)
    _write(d / "cmdline", "\0".join(argv) + "\0")
    comm = Path(argv[0]).name
    _write(d / "stat", f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 0\n")
    _write(d / "status", f"Name:\t{comm}\nPid:\t{pid}\nPPid:\t{ppid}\n")


def test_orphans_reports_only_processes_outside_the_jobs_own_tree(tmp_path) -> None:
    """added: orphans() over a fake delegated bgjob cgroup. Self, its
    ancestors (run.sh, bwrap) and descendants (CLI, MCP server, a foreground
    testrun leaf) are not orphans; a nohup'd pytest in box/agent and a
    detached testrun leaf in box/testrun-* are."""
    from src.model_router.job import orphans

    me = os.getpid()
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    unit = "system.slice/bgjob-20261025-x.service"
    _write(proc / str(me) / "cgroup", f"0::/{unit}/box/agent\n")
    _fake_proc(proc, 1, 0, ["/sbin/init"])
    _fake_proc(proc, 100, 1, ["bash", "run.sh"])
    _fake_proc(proc, 101, 100, ["bwrap", "--ro-bind", "/", "/"])
    _fake_proc(proc, me, 101, ["python", "scripts/agentjob"])
    _fake_proc(proc, 201, me, ["claude", "--output-format", "stream-json"])
    _fake_proc(proc, 202, 201, ["node", "mcp-server.js"])
    _fake_proc(proc, 203, 201, ["python", "-m", "pytest", "tests/unit/test_a.py"])
    _fake_proc(proc, 300, 1, ["python", "-m", "pytest", "-q", "tests"])
    _fake_proc(proc, 400, 1, ["python", "-m", "pytest", "tests/slow"])
    _write(cg / unit / "cgroup.procs", "")
    _write(cg / unit / "box" / "cgroup.procs", "")
    _write(
        cg / unit / "box" / "agent" / "cgroup.procs", f"100\n101\n{me}\n201\n202\n300\n"
    )
    _write(cg / unit / "box" / "testrun-1" / "cgroup.procs", "203\n")
    _write(cg / unit / "box" / "testrun-2" / "cgroup.procs", "400\n")

    found = orphans(proc_root=proc, cgroup_root=cg)

    assert {pid for pid, _ in found} == {300, 400}
    assert "pytest" in dict(found)[300]

    # The clean job: the same tree without the two leftovers -> nothing.
    _write(cg / unit / "box" / "agent" / "cgroup.procs", f"100\n101\n{me}\n201\n202\n")
    _write(cg / unit / "box" / "testrun-2" / "cgroup.procs", "")
    assert orphans(proc_root=proc, cgroup_root=cg) == []


# --------------------------------------------------------------------------
# dv-7ad9 phase 1: a usage-limit result exits QUOTA_EXIT (4) with the quota
# line on stderr; the model_calls row stays outcome=error.
# --------------------------------------------------------------------------


async def test_limit_result_exits_4_with_the_quota_line(monkeypatch, capsys) -> None:
    """added: the final ResultMessage is the CLI's weekly-limit error ->
    exit 4, stderr carries a line parse_quota_line reads back, outcome error.
    Other errors keep exit 1 (test_error_in_a_nudge_turn_...)."""
    from src.model_router.quota import parse_quota_line

    limit = "You've hit your weekly limit · resets 9am (UTC)"
    run, _, posted, _ = _multi_turn_job(
        monkeypatch, [[_result(limit, is_error=True, subtype="error_during_execution")]]
    )

    assert await run() == 4
    err = capsys.readouterr().err
    parsed = [p for p in map(parse_quota_line, err.splitlines()) if p is not None]
    assert parsed, err
    assert parsed[0].until.hour == 9 and parsed[0].until.minute == 0
    assert _call_row(posted)["outcome"] == "error"


# --------------------------------------------------------------------------
# dv-d63d phase 2: jobs on the route table; OpenRouter jobs.
#
# Contract pinned here (docs/research/dv-d63d/analysis.md, Phase 2):
# * ``JobSpec(..., route=<CallRoutes.resolve_job(...)>)``: the resolved job
#   route travels with the spec; no ``route`` = the subscription, as before.
# * An OpenRouter job route runs the Claude CLI with ``provider_env``: base
#   URL, the key-store key as auth token, an empty ANTHROPIC_API_KEY, the slug
#   as model and as subagent model.
# * Its ``model_calls`` row (through ``/internal/model_calls``): harness
#   ``claude``, provider ``openrouter``, cost from tokens x llm_pricing.json
#   (``cost_for``), never the CLI's Anthropic-priced ``total_cost_usd``.
# * Owner answer (2026-10-26): no automatic fallback; a failed job fails.
# --------------------------------------------------------------------------

OR_SLUG = "z-ai/glm-5.3"
OR_KEY = "sk-or-job-store"
OR_JOBS_YAML = f"""
jobs:
  pump_review:
    harness: openrouter
    models:
      openrouter: {OR_SLUG}
"""


def _or_key_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import src.security.keystore as keystore

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = SimpleNamespace(
        env=lambda service: (
            {"OPENROUTER_API_KEY": OR_KEY} if service == "openrouter" else {}
        )
    )
    monkeypatch.setattr(keystore, "default_keystore", lambda: store)


def _job_route(tmp_path: Path, name: str, body: str = OR_JOBS_YAML) -> Any:
    from src.model_router.routes import load_call_routes

    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return load_call_routes(path).resolve_job(name)


def _routed_spec(tmp_path: Path, route: Any, name: str = "pump-dv-x-review") -> JobSpec:
    return JobSpec(
        purpose="background_job",
        name=name,
        prompt="review it",
        model=route.model,
        route=route,  # type: ignore[call-arg]
        working_directory=tmp_path,
        full_access=True,
        unrestricted=True,
        profile="devops",
    )


def _or_stream(result: Any = None) -> List[Any]:
    return [
        SystemMessage(
            subtype="init",
            data={"claude_code_version": "2.1.310", "model": OR_SLUG},
        ),
        AssistantMessage(content=[TextBlock(text="PASS")], model=OR_SLUG),
        (
            result
            if result is not None
            else _job_result(
                result="PASS",
                # The CLI prices on the Anthropic table: wrong on OpenRouter.
                total_cost_usd=0.02,
                usage={"input_tokens": 1000, "output_tokens": 100},
            )
        ),
    ]


def _capturing_sdk(monkeypatch: pytest.MonkeyPatch, messages: List[Any]) -> List[Any]:
    built: List[Any] = []

    def make(options: Any) -> _FakeClient:
        built.append(options)
        return _FakeClient(messages)

    monkeypatch.setattr("src.model_router.job.ClaudeSDKClient", make)
    monkeypatch.setattr("src.model_router.job.orphans", lambda *a, **k: [])
    return built


def test_pump_review_on_openrouter_runs_the_cli_with_the_openrouter_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jobs.pump_review: openrouter -> the review stage's CLI talks to
    OpenRouter with the key-store key, and no inherited Anthropic key."""
    from src.model_router.routes import OPENROUTER_ANTHROPIC_BASE_URL

    _or_key_store(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-anthropic-key")
    route = _job_route(tmp_path, "pump_review")

    options = job_options(_routed_spec(tmp_path, route), BotLink("http://x:1", "s"))

    assert options.model == OR_SLUG
    assert options.env["ANTHROPIC_BASE_URL"] == OPENROUTER_ANTHROPIC_BASE_URL
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == OR_KEY
    assert options.env["ANTHROPIC_API_KEY"] == ""
    # Subagents and every tier alias stay on the slug, not on Anthropic sonnet.
    assert options.env["CLAUDE_CODE_SUBAGENT_MODEL"] == OR_SLUG
    assert options.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == OR_SLUG
    # The policy gate is still there.
    (matcher,) = options.hooks["PreToolUse"]
    assert len(matcher.hooks) == 1


def test_claude_job_route_keeps_the_subscription_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other rows of the same file stay on the subscription."""
    _or_key_store(monkeypatch)
    route = _job_route(tmp_path, "pump_implement")

    options = job_options(
        _routed_spec(tmp_path, route, "pump-dv-x-implement"),
        BotLink("http://x:1", "s"),
    )

    assert options.model == "opus"
    assert "ANTHROPIC_BASE_URL" not in options.env
    assert "ANTHROPIC_AUTH_TOKEN" not in options.env
    assert options.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"


async def test_openrouter_job_row_is_claude_harness_openrouter_provider_and_priced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_db: Any
) -> None:
    _or_key_store(monkeypatch)
    route = _job_route(tmp_path, "pump_review")
    _capturing_sdk(monkeypatch, _or_stream())
    link = _AppLink(_bot_app(bot_db))
    out = io.StringIO()

    code = await run_job(_routed_spec(tmp_path, route), link, out=out)

    assert code == 0 and "PASS" in out.getvalue()
    (call,) = await _rows(bot_db, "model_calls")
    assert call["harness"] == "claude"
    assert call["provider"] == "openrouter"
    assert call["model"] == OR_SLUG
    assert (call["input_tokens"], call["output_tokens"]) == (1000, 100)
    # glm-5.3: 2.8 in / 8.8 out per 1M tokens; not the CLI's 0.02.
    assert call["cost_usd"] == pytest.approx(1000 * 2.8e-6 + 100 * 8.8e-6)


async def test_subscription_job_row_is_not_marked_openrouter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_db: Any
) -> None:
    """Guard: a job with no route keeps today's row (Claude, the CLI's cost)."""
    _scripted_sdk(monkeypatch, _job_stream())
    link = _AppLink(_bot_app(bot_db))

    assert await run_job(_spec(tmp_path), link, out=io.StringIO()) == 0

    (call,) = await _rows(bot_db, "model_calls")
    assert call["harness"] == "claude"
    assert call["provider"] != "openrouter"
    assert call["cost_usd"] == pytest.approx(0.02)


async def test_failed_openrouter_job_is_not_retried_on_another_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_db: Any
) -> None:
    """added: owner answer 2026-10-26 -- no automatic fallback; a failure is
    seen as a failure on its own provider, never hidden by a second run."""
    _or_key_store(monkeypatch)
    route = _job_route(tmp_path, "pump_review")
    failed = _job_result(
        subtype="error_during_execution",
        is_error=True,
        result="402 insufficient credits",
        usage={"input_tokens": 10, "output_tokens": 0},
    )
    built = _capturing_sdk(monkeypatch, _or_stream(failed))
    link = _AppLink(_bot_app(bot_db))

    code = await run_job(_routed_spec(tmp_path, route), link, out=io.StringIO())

    assert code != 0
    assert len(built) == 1  # one client, one provider
    (call,) = await _rows(bot_db, "model_calls")
    assert call["outcome"] == "error"
    assert call["provider"] == "openrouter"
