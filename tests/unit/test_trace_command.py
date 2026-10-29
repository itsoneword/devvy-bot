"""Harness trace, phase 3 (dv-2260): ``/trace`` and ``scripts/trace`` show a
turn's question, answer and tool-call tree.

The data is real: turns run through ``ModelRouter.run_turn`` on the real
``ClaudeIntegration`` (scripted SDK stream) into real SQLite, exactly as in
``test_model_router_trace.py``; bot turns are then saved with
``Storage.save_claude_interaction``. The command is driven through the
handler the orchestrator actually registers (``CommandHandler("trace")``,
wrapped by ``_owner_only`` and ``_inject_deps``), with the real ``Storage``
in the orchestrator's deps.

Contract these tests pin (docs/research/dv-2260/analysis.md, "Phase 3" and
design point 9):

* ``trace`` is a ``COMMAND_TABLE`` command and in ``OWNER_ONLY_COMMANDS``:
  not in a guest's menu or ``/help``; a guest calling it is refused.
* ``/trace`` (no argument) = the newest turn: header with the question and
  the answer, then an indented tree, one line per tool call:
  ``✓`` ok, ``✗`` error, ``⏳ ... detached`` for a backgrounded subagent,
  and the call's duration (``1.2s`` style, or ``1234ms``). A subagent's
  tools are indented deeper than its Agent line.
* ``/trace <id>`` -- a bare integer is a ``model_calls`` id, so a pump /
  bgjob call (no ``messages`` row) shows its tree.
* A reply whose text would exceed 4000 chars is sent as a ``.txt``
  document (``reply_document``) holding the full tree.
* ``scripts/trace <model_call_id>`` prints the same tree from the data
  root's ``bot.db`` (``DEVVY_DATA_ROOT``).
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.bot.orchestrator import (
    COMMAND_TABLE,
    OWNER_ONLY_COMMANDS,
    MessageOrchestrator,
)
from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeSDKManager
from src.claude.session import SessionManager
from src.claude.tool_context import AgentServices
from src.config import create_test_config
from src.config.settings import Settings
from src.model_router import ModelRouter
from src.model_router.harness import HarnessVersions
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.model_calls import ModelCallRepository
from src.storage.session_storage import SQLiteSessionStorage
from src.storage.tool_calls import ToolCallRepository
from src.utils.paths import data_layout

REPO = Path(__file__).resolve().parents[2]
OWNER_ID = 111
GUEST_ID = 222

QUESTION = "How many sections does the README have?"
ANSWER = "It has 3 sections."
SUBAGENT_FINAL = "Subagent report: README has 3 sections."
DURATION = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b")
MARKS = ("✓", "✗", "⏳")


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _Bus:
    async def publish(self, event: Any) -> None:
        pass


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


# --------------------------------------------------------------- turn data


def _result(session_id: str, text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.0,
        result=text,
    )


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


async def _turn(
    settings: Settings,
    storage: Storage,
    workdir: Path,
    messages: List[Any],
    *,
    purpose: str = "chat",
    prompt: str = QUESTION,
    save: bool = True,
) -> Dict[str, Any]:
    """One real router turn; a bot turn (save=True) also gets its message row.
    Returns {"model_call_id", "message_id"}."""
    manager = ClaudeSDKManager(
        settings, security_validator=SecurityValidator(workdir.parent)
    )
    manager.attach_agent_services(AgentServices(event_bus=_Bus(), task_repo=None))
    sessions = SessionManager(settings, SQLiteSessionStorage(storage.db_manager))
    backend = ClaudeIntegration(settings, sdk_manager=manager, session_manager=sessions)
    router = ModelRouter(
        backends={"claude": backend},
        recorder=ModelCallRepository(storage.db_manager),
        tool_calls=ToolCallRepository(storage.db_manager),
        versions=_Versions(),
    )
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_mock_client(messages),
        ):
            response = await router.run_turn(
                "claude",
                purpose=purpose,
                prompt=prompt,
                working_directory=workdir,
                user_id=OWNER_ID,
                force_new=True,
                model="haiku",
                role="owner",
            )
    message_id = None
    if save:
        message_id = await storage.save_claude_interaction(
            user_id=OWNER_ID,
            session_id=response.session_id,
            prompt=prompt,
            response=response,
        )
    return {"model_call_id": response.model_call_id, "message_id": message_id}


def _read_agent_bash_stream(session_id: str = "sess-chat") -> List[Any]:
    """Main agent: Read (ok) + Agent (subagent runs Grep) + Bash (fails)."""
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
                ToolUseBlock(id="b1", name="Bash", input={"command": "wc -l nope"}),
            ],
            model="haiku",
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="r1", content="# A\n# B\n# C"),
                ToolResultBlock(
                    tool_use_id="b1",
                    content="wc: nope: No such file or directory",
                    is_error=True,
                ),
            ]
        ),
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
            content=[TextBlock(text=SUBAGENT_FINAL)],
            model="haiku",
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
        AssistantMessage(content=[TextBlock(text=ANSWER)], model="haiku"),
        _result(session_id, ANSWER),
    ]


def _single_tool_stream(
    session_id: str, tool: str, inp: Dict[str, Any], answer: str
) -> List[Any]:
    return [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name=tool, input=inp)], model="haiku"
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        AssistantMessage(content=[TextBlock(text=answer)], model="haiku"),
        _result(session_id, answer),
    ]


async def _set_duration(storage: Storage, tool_use_id: str, ms: int) -> None:
    """Pin one measured duration so its rendering can be asserted (the
    scripted stream runs in ~0 ms)."""
    async with storage.db_manager.get_connection() as conn:
        await conn.execute(
            "UPDATE tool_calls SET duration_ms = ? WHERE tool_use_id = ?",
            (ms, tool_use_id),
        )
        await conn.commit()


# ---------------------------------------------------------- command driving


def _orchestrator(tmp_path: Path, storage: Any) -> MessageOrchestrator:
    cfg = create_test_config(approved_directory=str(tmp_path))
    deps = {
        "claude_integration": MagicMock(),
        "storage": storage,
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": None,
        "profile_registry": None,
    }
    return MessageOrchestrator(cfg, deps)


def _registered_handler(orchestrator: MessageOrchestrator, name: str) -> Any:
    from telegram.ext import CommandHandler

    app = MagicMock()
    orchestrator.register_handlers(app)
    for call in app.add_handler.call_args_list:
        handler = call[0][0]
        if isinstance(handler, CommandHandler) and name in handler.commands:
            return handler.callback
    raise AssertionError(f"/{name} is not registered")


class _Sent:
    """Everything the handler sent: texts and (filename, content) documents."""

    def __init__(self) -> None:
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.message.reply_document = AsyncMock()
        self.message.reply_html = AsyncMock()
        self.message.chat_id = OWNER_ID
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot.send_document = AsyncMock()

    def texts(self) -> List[str]:
        out: List[str] = []
        for mock in (
            self.message.reply_text,
            self.message.reply_html,
            self.bot.send_message,
        ):
            for call in mock.await_args_list:
                text = call.kwargs.get("text")
                if text is None:
                    args = [a for a in call.args if isinstance(a, str)]
                    text = args[-1] if args else ""
                out.append(_plain(text))
        for _name, _content, caption in self._docs():
            if caption:
                out.append(_plain(caption))
        return out

    def documents(self) -> List[tuple]:
        return [(n, c) for n, c, _cap in self._docs()]

    def _docs(self) -> List[tuple]:
        docs = []
        for mock in (self.message.reply_document, self.bot.send_document):
            for call in mock.await_args_list:
                doc = call.kwargs.get("document")
                if doc is None:
                    rest = [a for a in call.args if not isinstance(a, int)]
                    doc = rest[0] if rest else None
                name, content = _read_document(doc)
                name = call.kwargs.get("filename") or name
                docs.append((name or "", content, call.kwargs.get("caption")))
        return docs

    def everything(self) -> str:
        return "\n".join(self.texts() + [c for _n, c in self.documents()])


def _plain(text: str) -> str:
    """Telegram HTML -> what the owner reads."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def _read_document(doc: Any) -> tuple:
    name: Optional[str] = None
    data: Any = doc
    if hasattr(doc, "input_file_content"):  # telegram.InputFile
        name = getattr(doc, "filename", None)
        data = doc.input_file_content
    if isinstance(data, (str, Path)) and Path(str(data)).is_file():
        name = name or Path(str(data)).name
        data = Path(str(data)).read_bytes()
    elif hasattr(data, "read"):
        name = name or getattr(data, "name", None)
        if hasattr(data, "seek"):
            data.seek(0)
        data = data.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return name, str(data or "")


async def _run_trace(
    tmp_path: Path,
    storage: Any,
    args: Optional[List[str]] = None,
    *,
    role: str = "owner",
    user_id: int = OWNER_ID,
) -> _Sent:
    orchestrator = _orchestrator(tmp_path, storage)
    handler = _registered_handler(orchestrator, "trace")
    sent = _Sent()
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = None
    update.effective_chat.id = user_id
    update.message = sent.message
    update.effective_message = sent.message
    context = MagicMock()
    context.args = list(args or [])
    context.user_data = {"role": role, "_prefs_loaded": True}
    auth_manager = MagicMock()
    auth_manager.get_user_role = MagicMock(return_value=role)
    context.bot_data = {"auth_manager": auth_manager}
    context.bot = sent.bot
    await handler(update, context)
    return sent


def _tool_line(text: str, tool: str) -> str:
    """The tree line of ``tool`` (a line with a status mark and its name)."""
    for line in text.splitlines():
        if re.search(rf"\b{re.escape(tool)}\b", line) and any(m in line for m in MARKS):
            return line
    raise AssertionError(f"no tree line for {tool!r} in:\n{text}")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t│├└─·"))


# --------------------------------------------------------------------------
# Approved scenarios
# --------------------------------------------------------------------------


async def test_trace_shows_last_turn_question_answer_and_indented_tree(
    tmp_path, settings, storage, workdir
) -> None:
    """/trace = the newest turn: its question, its answer, and the tool tree
    with ✓/✗ and each call's time; the subagent's Grep sits under Agent."""
    await _turn(
        settings,
        storage,
        workdir,
        _single_tool_stream("sess-old", "Glob", {"pattern": "*.md"}, "Old answer."),
        prompt="An older question",
    )
    await _turn(settings, storage, workdir, _read_agent_bash_stream())
    await _set_duration(storage, "r1", 1234)

    sent = await _run_trace(tmp_path, storage)
    text = sent.everything()

    assert QUESTION in text
    assert ANSWER in text
    # The newest turn, not the older one.
    assert "An older question" not in text
    assert "Glob" not in text

    read = _tool_line(text, "Read")
    agent = _tool_line(text, "Agent")
    grep = _tool_line(text, "Grep")
    bash = _tool_line(text, "Bash")
    assert "✓" in read and "✓" in agent and "✓" in grep
    assert "✗" in bash and "✓" not in bash
    assert re.search(r"1\.2\s?s|1234\s?ms", read), read
    for line in (agent, grep, bash):
        assert DURATION.search(line), line
    # Indented tree: the subagent's tool is deeper than the Agent call,
    # main-agent calls are level with each other.
    assert _indent(grep) > _indent(agent)
    assert _indent(read) == _indent(agent) == _indent(bash)
    lines = text.splitlines()
    assert lines.index(agent) < lines.index(grep)


async def test_trace_model_call_id_shows_job_tree_without_message(
    tmp_path, settings, storage, workdir
) -> None:
    """/trace <model_call id> of a pump/bgjob call (no messages row) shows
    that call's tree; a bare id is a model_calls id, not a message id."""
    chat = await _turn(
        settings,
        storage,
        workdir,
        _single_tool_stream("sess-chat", "Glob", {"pattern": "*.md"}, "Chat."),
        prompt="A chat question",
    )
    job = await _turn(
        settings,
        storage,
        workdir,
        _single_tool_stream(
            "sess-job", "Bash", {"command": "scripts/taskpump tick"}, "Pump done."
        ),
        purpose="background_job",
        prompt="pump: implement dv-0000",
        save=False,
    )
    assert job["model_call_id"] != chat["message_id"]

    sent = await _run_trace(tmp_path, storage, [str(job["model_call_id"])])
    text = sent.everything()

    bash = _tool_line(text, "Bash")
    assert "✓" in bash
    assert "taskpump tick" in text
    # Not the chat turn (whose message id is a different number).
    assert "A chat question" not in text
    assert "Glob" not in text


async def test_trace_shows_detached_subagent_as_hourglass(
    tmp_path, settings, storage, workdir
) -> None:
    """A subagent still running at turn end (status detached) is ⏳ detached."""
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
        _result("sess-bg", "Started it."),
    ]
    ids = await _turn(settings, storage, workdir, stream, prompt="dig in background")

    sent = await _run_trace(tmp_path, storage, [str(ids["model_call_id"])])
    agent = _tool_line(sent.everything(), "Agent")
    assert "⏳" in agent
    assert "detached" in agent
    assert "✓" not in agent and "✗" not in agent


async def test_trace_long_tree_is_sent_as_txt_file(
    tmp_path, settings, storage, workdir
) -> None:
    """A tree too long for one message (> 4000 chars) goes as a .txt file
    holding the whole tree; no text message carries an over-long body."""
    n = 120
    uses = [
        ToolUseBlock(
            id=f"r{i}",
            name="Read",
            input={"file_path": f"/srv/project/module_{i:03d}/file_{i:03d}.py"},
        )
        for i in range(n)
    ]
    results = [
        ToolResultBlock(tool_use_id=f"r{i}", content=f"contents of file {i}")
        for i in range(n)
    ]
    stream = [
        AssistantMessage(content=uses, model="haiku"),
        UserMessage(content=results),
        AssistantMessage(content=[TextBlock(text="Read them all.")], model="haiku"),
        _result("sess-long", "Read them all."),
    ]
    await _turn(settings, storage, workdir, stream, prompt="read everything")

    sent = await _run_trace(tmp_path, storage)

    docs = sent.documents()
    assert len(docs) == 1, sent.texts()
    name, content = docs[0]
    assert name.endswith(".txt"), name
    assert "read everything" in content
    assert "module_000" in content and f"module_{n - 1:03d}" in content
    assert sum(1 for line in content.splitlines() if "Read" in line) >= n
    for text in sent.texts():
        assert len(text) <= 4096


async def test_trace_is_owner_only_menu_help_and_refusal(
    tmp_path, settings, storage, workdir
) -> None:
    """Non-owner: /trace is not in their menu or /help, and calling it is
    refused without showing any trace. The owner's /help lists it."""
    assert "trace" in {spec.name for spec in COMMAND_TABLE}
    assert "trace" in OWNER_ONLY_COMMANDS

    orchestrator = _orchestrator(tmp_path, storage)
    guest_menu = {c.command for c in await orchestrator.get_bot_commands("guest")}
    assert "trace" not in guest_menu

    async def help_text(role: str) -> str:
        update = MagicMock()
        update.effective_user.id = OWNER_ID if role == "owner" else GUEST_ID
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {"role": role}
        context.bot_data = {"profile_registry": None}
        await orchestrator.agentic_help(update, context)
        return update.message.reply_text.call_args[0][0]

    assert "/trace" not in await help_text("guest")
    assert "/trace" in await help_text("owner")

    await _turn(settings, storage, workdir, _read_agent_bash_stream())
    sent = await _run_trace(tmp_path, storage, role="guest", user_id=GUEST_ID)
    text = sent.everything()
    assert "owner-only" in text
    assert QUESTION not in text and ANSWER not in text
    assert "Read" not in text and not sent.documents()


# --------------------------------------------------------------------------
# Added scenarios
# --------------------------------------------------------------------------


async def test_trace_with_no_traced_turns_says_so(tmp_path, storage) -> None:
    """Empty DB: /trace answers (one short message), no crash, no file."""
    sent = await _run_trace(tmp_path, storage)
    texts = sent.texts()
    assert len(texts) == 1 and texts[0].strip()
    assert not sent.documents()
    assert not any(m in texts[0] for m in MARKS)


async def test_trace_unknown_id_says_not_found(
    tmp_path, settings, storage, workdir
) -> None:
    """An id with no model_calls row: a short 'not found'-style reply, not
    some other turn's tree."""
    await _turn(settings, storage, workdir, _read_agent_bash_stream())
    sent = await _run_trace(tmp_path, storage, ["987654"])
    text = sent.everything()
    assert text.strip()
    assert QUESTION not in text
    assert "Read" not in text and "Agent" not in text
    assert not sent.documents()


async def test_scripts_trace_prints_a_calls_tree(tmp_path, settings, workdir) -> None:
    """scripts/trace <model_call_id> reads the data root's bot.db and prints
    the question, the answer and the tree."""
    root = tmp_path / "data"
    db_path = data_layout(root).bot_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    st = Storage(f"sqlite:///{db_path}")
    await st.initialize()
    try:
        ids = await _turn(settings, st, workdir, _read_agent_bash_stream())
    finally:
        await st.close()

    env = {**os.environ, "DEVVY_DATA_ROOT": str(root)}
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "trace"), str(ids["model_call_id"])],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert QUESTION in out and ANSWER in out
    assert "✓" in _tool_line(out, "Grep")
    assert "✗" in _tool_line(out, "Bash")
    assert _indent(_tool_line(out, "Grep")) > _indent(_tool_line(out, "Agent"))
