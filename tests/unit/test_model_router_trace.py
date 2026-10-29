"""Harness trace, phase 1 (dv-2260): every turn's tool calls land in
``tool_calls``, written by the model router beside the ``model_calls`` row.

Driven through the real entry point: ``ModelRouter.run_turn`` on the real
backends (``ClaudeIntegration`` over a ``ClaudeSDKManager`` with a scripted SDK
stream; ``CodexCliIntegration`` over a fake ``codex`` binary), real SQLite.

Contract these tests pin (docs/research/dv-2260/analysis.md):

* ``ModelRouter(..., recorder=ModelCallRepository(db),
  tool_calls=ToolCallRepository(db))`` -- ``src.storage.tool_calls``.
* ``tool_calls`` columns: model_call_id, message_id, session_id, seq,
  tool_use_id, parent_tool_use_id (NULL = main agent), tool, subagent_type,
  input (JSON), status (ok|error|detached|unfinished), error, started_at,
  duration_ms, result_bytes, result_preview.
* previews <= 200 chars, Agent/Task rows <= 4000 (the subagent's final text),
  secrets scrubbed; rows written on the success AND the exception path.
* ``Storage.save_claude_interaction`` links the turn's rows to its message
  (via ``response.model_call_id``).
"""

from __future__ import annotations

import json
import shlex
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeSDKManager
from src.claude.session import SessionManager
from src.claude.tool_context import AgentServices
from src.config.settings import Settings
from src.model_router import ModelRouter
from src.model_router.harness import HarnessVersions
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.model_calls import ModelCallRepository
from src.storage.session_storage import SQLiteSessionStorage

USER_ID = 111

# Secrets the scrubber must remove (log_redaction.redact, extended).
TG_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"  # fake
SK_KEY = "sk-ant-api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa"  # fake


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _Bus:
    def __init__(self) -> None:
        self.events: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _denylist_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "repo"
    wd.mkdir()
    return wd


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        approved_directory=tmp_path,
    )


@pytest.fixture
async def storage(tmp_path: Path):
    st = Storage(f"sqlite:///{tmp_path / 'bot.db'}")
    await st.initialize()
    yield st
    await st.close()


def _router(storage: Storage, backends: Dict[str, Any]) -> ModelRouter:
    from src.storage.tool_calls import ToolCallRepository

    return ModelRouter(
        backends=backends,
        recorder=ModelCallRepository(storage.db_manager),
        tool_calls=ToolCallRepository(storage.db_manager),
        versions=_Versions(),
    )


def _claude_backend(settings: Settings, storage: Storage, workdir: Path) -> Any:
    manager = ClaudeSDKManager(
        settings, security_validator=SecurityValidator(workdir.parent)
    )
    manager.attach_agent_services(AgentServices(event_bus=_Bus(), task_repo=None))
    sessions = SessionManager(settings, SQLiteSessionStorage(storage.db_manager))
    return ClaudeIntegration(settings, sdk_manager=manager, session_manager=sessions)


def _mock_client(messages: List[Any]) -> Any:
    def factory(options: Any) -> Any:
        client = AsyncMock()

        async def receive() -> Any:
            for m in messages:
                yield m

        client._query = AsyncMock()
        client._query.receive_messages = receive
        return client

    return factory


def _result(**kw: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-trace",
        total_cost_usd=0.0,
        result="done",
    )
    base.update(kw)
    return ResultMessage(**base)


async def _claude_turn(
    settings: Settings, storage: Storage, workdir: Path, messages: List[Any]
) -> Any:
    backend = _claude_backend(settings, storage, workdir)
    router = _router(storage, {"claude": backend})
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_mock_client(messages),
        ):
            return await router.run_turn(
                "claude",
                purpose="chat",
                prompt="hi",
                working_directory=workdir,
                user_id=USER_ID,
                force_new=True,
                model="haiku",
                role="owner",
            )


async def _tool_rows(storage: Storage, where: str = "1=1", args: tuple = ()) -> list:
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute(
            f"SELECT * FROM tool_calls WHERE {where} ORDER BY seq, id", args
        )
        return [dict(r) for r in await cur.fetchall()]


async def _model_calls(storage: Storage) -> list:
    async with storage.db_manager.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


def _by_id(rows: list) -> Dict[Optional[str], dict]:
    return {r["tool_use_id"]: r for r in rows}


def _input(row: dict) -> Dict[str, Any]:
    raw = row["input"]
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


SUBAGENT_FINAL = "Subagent report: README has 3 sections."


def _read_agent_stream(
    *, read_result: str = "line1\nline2", agent_result: str = SUBAGENT_FINAL
) -> List[Any]:
    """Main agent: Read + Agent; the subagent does a Grep of its own."""
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(id="r1", name="Read", input={"file_path": "/x/README"}),
                ToolUseBlock(
                    id="a1",
                    name="Agent",
                    input={
                        "subagent_type": "general-purpose",
                        "description": "scan",
                        "prompt": "count sections",
                    },
                ),
            ],
            model="haiku",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="r1", content=read_result)]),
        AssistantMessage(
            content=[ToolUseBlock(id="g1", name="Grep", input={"pattern": "^#"})],
            model="haiku",
            parent_tool_use_id="a1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="g1", content="# A\n# B\n# C")],
            parent_tool_use_id="a1",
        ),
        AssistantMessage(
            content=[TextBlock(text=agent_result)],
            model="haiku",
            parent_tool_use_id="a1",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="a1",
                    content=[{"type": "text", "text": agent_result}],
                )
            ]
        ),
        AssistantMessage(content=[TextBlock(text="It has 3 sections.")], model="haiku"),
        _result(),
    ]


# --------------------------------------------------------------------------
# Approved scenarios
# --------------------------------------------------------------------------


async def test_read_and_agent_rows_linked_to_message_with_subagent_tree(
    settings, storage, workdir
) -> None:
    """Bot turn with Read+Agent: rows linked to message, subagent tools under
    the Agent row."""
    response = await _claude_turn(settings, storage, workdir, _read_agent_stream())

    calls = await _model_calls(storage)
    assert len(calls) == 1
    assert response.model_call_id == calls[0]["id"]

    message_id = await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="hi",
        response=response,
    )

    rows = await _tool_rows(storage, "message_id = ?", (message_id,))
    by_id = _by_id(rows)
    assert set(by_id) == {"r1", "a1", "g1"}
    assert all(r["model_call_id"] == calls[0]["id"] for r in rows)

    read, agent, grep = by_id["r1"], by_id["a1"], by_id["g1"]
    assert (read["tool"], read["parent_tool_use_id"], read["status"]) == (
        "Read",
        None,
        "ok",
    )
    assert _input(read)["file_path"] == "/x/README"
    assert (agent["tool"], agent["parent_tool_use_id"]) == ("Agent", None)
    assert agent["subagent_type"] == "general-purpose"
    assert agent["status"] == "ok"
    # The subagent's own tool hangs under the Agent call.
    assert (grep["tool"], grep["parent_tool_use_id"], grep["status"]) == (
        "Grep",
        "a1",
        "ok",
    )
    # Order within the call is the order the calls were made.
    assert [r["tool_use_id"] for r in rows] == ["r1", "a1", "g1"]
    assert len({r["seq"] for r in rows}) == 3
    for r in rows:
        assert r["duration_ms"] is not None and r["duration_ms"] >= 0
        assert r["started_at"] is not None
    assert read["result_bytes"] == len("line1\nline2".encode())


async def test_agent_row_keeps_subagent_final_text_previews_capped_and_scrubbed(
    settings, storage, workdir
) -> None:
    """Agent row holds the subagent's final text (<=4000); other previews
    <=200; secrets scrubbed from previews and inputs."""
    long_read = f"token=/bot{TG_TOKEN}/getMe " + "x" * 1000
    long_final = f"Findings. key {SK_KEY} found in .env. " + "y" * 6000
    stream = _read_agent_stream(read_result=long_read, agent_result=long_final)
    # A Bash call whose input carries a secret, and a short subagent result.
    stream[0].content.append(
        ToolUseBlock(
            id="b1",
            name="Bash",
            input={"command": f"curl https://api.telegram.org/bot{TG_TOKEN}/getMe"},
        )
    )
    stream[1].content.append(ToolResultBlock(tool_use_id="b1", content="{ok:true}"))

    await _claude_turn(settings, storage, workdir, stream)
    by_id = _by_id(await _tool_rows(storage))

    read, agent, bash = by_id["r1"], by_id["a1"], by_id["b1"]
    assert 0 < len(read["result_preview"]) <= 200
    assert read["result_bytes"] >= 1000
    assert TG_TOKEN not in read["result_preview"]

    assert 200 < len(agent["result_preview"]) <= 4000
    assert agent["result_preview"].startswith("Findings.")
    assert SK_KEY not in agent["result_preview"]

    assert TG_TOKEN not in json.dumps(_input(bash))
    assert TG_TOKEN not in (bash["result_preview"] or "")
    for r in by_id.values():
        for v in _input(r).values():
            if isinstance(v, str):
                assert len(v) <= 300


async def test_agent_row_short_final_text_kept_verbatim(
    settings, storage, workdir
) -> None:
    """The subagent's final text, when short, is the Agent row's preview."""
    await _claude_turn(settings, storage, workdir, _read_agent_stream())
    agent = _by_id(await _tool_rows(storage))["a1"]
    assert agent["result_preview"] == SUBAGENT_FINAL


async def test_interrupted_turn_keeps_tool_rows_unpaired_marked_unfinished(
    settings, storage, workdir
) -> None:
    """Interrupted turn (stream dies before ResultMessage): the tool rows seen
    so far are written; the call that never got a result is ``unfinished``."""

    class _Interrupted(Exception):
        pass

    stream: List[Any] = [
        AssistantMessage(
            content=[ToolUseBlock(id="r1", name="Read", input={"file_path": "/a"})],
            model="haiku",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="r1", content="aaa")]),
        AssistantMessage(
            content=[ToolUseBlock(id="b1", name="Bash", input={"command": "sleep 99"})],
            model="haiku",
        ),
    ]

    def factory(options: Any) -> Any:
        client = AsyncMock()

        async def receive() -> Any:
            for m in stream:
                yield m
            raise _Interrupted("stopped by /stop")

        client._query = AsyncMock()
        client._query.receive_messages = receive
        return client

    backend = _claude_backend(settings, storage, workdir)
    router = _router(storage, {"claude": backend})
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            with pytest.raises(BaseException):
                await router.run_turn(
                    "claude",
                    purpose="chat",
                    prompt="hi",
                    working_directory=workdir,
                    user_id=USER_ID,
                    force_new=True,
                    model="haiku",
                    role="owner",
                )

    calls = await _model_calls(storage)
    assert len(calls) == 1 and calls[0]["outcome"] == "error"
    by_id = _by_id(await _tool_rows(storage, "model_call_id = ?", (calls[0]["id"],)))
    assert set(by_id) == {"r1", "b1"}
    assert by_id["r1"]["status"] == "ok"
    assert by_id["b1"]["status"] == "unfinished"
    assert by_id["b1"]["tool"] == "Bash"


# --- Codex ----------------------------------------------------------------

CODEX_FAIL = "MCP tool call requires approval, but approval policy is never"


def _fake_codex(tmp_path: Path) -> Path:
    def item(item_id: str, kind: str, start: Dict[str, Any], done: Dict[str, Any]):
        base = {"id": item_id, "type": kind, **start}
        return [
            {"type": "item.started", "item": {**base, "status": "in_progress"}},
            {"type": "item.completed", "item": {**base, **done}},
        ]

    events = [
        {"type": "thread.started", "thread_id": "thread-trace"},
        *item(
            "c1",
            "command_execution",
            {"command": "ls -la"},
            {
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "README.md\nsrc\n" + "z" * 900,
            },
        ),
        *item(
            "m1",
            "mcp_tool_call",
            {"server": "agent", "tool": "memory_list", "arguments": {}},
            {"status": "failed", "error": {"message": CODEX_FAIL}},
        ),
        *item(
            "m2",
            "mcp_tool_call",
            {"server": "agent", "tool": "notify", "arguments": {}},
            {"status": "completed", "result": {"content": []}},
        ),
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    script = tmp_path / "fake_codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        + "".join(f"echo {shlex.quote(json.dumps(e))}\n" for e in events)
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_codex_turn_writes_flat_rows_with_status_and_output_preview(
    settings, storage, workdir, tmp_path
) -> None:
    """Codex turn: flat rows (no parent), ok/error with the error text, and
    the command's output as a <=200-char preview."""
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
        session_manager=SessionManager(
            settings, SQLiteSessionStorage(storage.db_manager)
        ),
    )
    router = _router(storage, {"openai_codex": integ})
    response = await router.run_turn(
        "openai_codex",
        purpose="chat",
        prompt="p",
        working_directory=workdir,
        user_id=USER_ID,
        force_new=True,
    )
    calls = await _model_calls(storage)
    assert len(calls) == 1 and response.model_call_id == calls[0]["id"]

    message_id = await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="p",
        response=response,
    )
    rows = await _tool_rows(storage, "message_id = ?", (message_id,))
    assert len(rows) == 3
    assert all(r["parent_tool_use_id"] is None for r in rows)
    assert all(r["model_call_id"] == calls[0]["id"] for r in rows)
    by_tool = {r["tool"].rsplit("__", 1)[-1]: r for r in rows}

    shell = by_tool["Bash"]
    assert shell["status"] == "ok"
    assert _input(shell)["command"] == "ls -la"
    assert shell["result_preview"].startswith("README.md")
    assert len(shell["result_preview"]) <= 200
    assert shell["result_bytes"] >= 900

    failed = by_tool["memory_list"]
    assert failed["status"] == "error"
    assert failed["error"] == CODEX_FAIL
    assert by_tool["notify"]["status"] == "ok"
    assert by_tool["notify"]["error"] is None


# --------------------------------------------------------------------------
# Added scenarios
# --------------------------------------------------------------------------


async def test_backgrounded_subagent_still_running_at_turn_end_marked_detached(
    settings, storage, workdir
) -> None:
    """Owner answer (3): a subagent launched in the background and not done
    when the turn ends is recorded as ``detached``; the turn is unchanged."""
    stream = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="a1",
                    name="Agent",
                    input={"subagent_type": "Explore", "prompt": "dig",
                           "run_in_background": True},
                )  # fmt: skip
            ],
            model="haiku",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="a1",
                    content=[
                        {"type": "text",
                         "text": "Async agent launched successfully. agentId: x1"}
                    ],
                )  # fmt: skip
            ]
        ),
        AssistantMessage(content=[TextBlock(text="Started it.")], model="haiku"),
        _result(),
    ]
    response = await _claude_turn(settings, storage, workdir, stream)
    assert not response.is_error
    agent = _by_id(await _tool_rows(storage))["a1"]
    assert agent["tool"] == "Agent"
    assert agent["subagent_type"] == "Explore"
    assert agent["status"] == "detached"


async def test_turn_without_tools_writes_no_tool_rows(
    settings, storage, workdir
) -> None:
    """Empty case: a plain answer writes its model_calls row and no
    tool_calls rows (and saving the message does not fail)."""
    stream = [
        AssistantMessage(content=[TextBlock(text="hi")], model="haiku"),
        _result(),
    ]
    response = await _claude_turn(settings, storage, workdir, stream)
    assert len(await _model_calls(storage)) == 1
    await storage.save_claude_interaction(
        user_id=USER_ID, session_id=response.session_id, prompt="hi", response=response
    )
    assert await _tool_rows(storage) == []
