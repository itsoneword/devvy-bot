"""dv-6f2a: agent_tools.py tool schemas mark only genuinely-required params.

Sibling of ``TestOptionalParams`` in ``test_user_data_tools.py`` (dv-9679).
Same bug, same proof obligation: the SDK's dict shorthand emits
``"required": list(properties)``, so every declared key becomes mandatory and
the model must invent the ones it wasn't told. The repo-wide guard at the
bottom is the part that keeps the class from coming back in a third file.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, Dict, List

import pytest

from src.claude.agent_tools import AgentServices, build_agent_mcp_server
from src.claude.tool_schema import tool_schema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class _StubScheduler:
    """Records add_job kwargs so a call's effect is inspectable."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def add_job(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "job-1"

    async def list_jobs(self) -> List[Any]:
        return []


class _StubTaskRepo:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def add_task(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "dv-0001"


class _StubEventBus:
    async def publish(self, event: Any) -> None:
        return None


@pytest.fixture
def scheduler() -> _StubScheduler:
    return _StubScheduler()


@pytest.fixture
def task_repo() -> _StubTaskRepo:
    return _StubTaskRepo()


@pytest.fixture
def tools(
    scheduler: _StubScheduler,
    task_repo: _StubTaskRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, Any]:
    """Every tool the agent server mounts, by name.

    ``create_sdk_mcp_server`` returns an opaque dict, so intercept it and keep
    the ``tools`` list it was handed — that list is what the SDK turns into
    wire schemas, which is exactly what these tests are about.

    ``role="owner"`` so the job tools mount too; ``user_data`` stays None so
    this covers agent_tools.py only (user_data_tools.py has its own suite).
    """
    captured: List[Any] = []

    def _capture(*, name: str, version: str, tools: List[Any]) -> Dict[str, Any]:
        captured.extend(tools)
        return {"type": "sdk", "name": name, "instance": object()}

    monkeypatch.setattr("src.claude.agent_tools.create_sdk_mcp_server", _capture)

    build_agent_mcp_server(
        AgentServices(
            event_bus=_StubEventBus(),
            task_repo=task_repo,
            scheduler=scheduler,
            memory_writer=object(),
            memory_repo=object(),
            message_repo=object(),
            audit_memory_writer=object(),
        ),
        profile_slug="devops",
        working_directory=pathlib.Path("/tmp"),
        requester_chat_id=1,
        memory_agent_id="devops#u1",
        user_id=1,
        role="owner",
    )
    return {t.name: t for t in captured}


class TestOptionalParams:
    """Ported from test_user_data_tools.py — same assertions, sibling file."""

    def test_schemas_are_explicit_not_shorthand(self, tools: Dict[str, Any]) -> None:
        """The SDK honours `required` only on a schema carrying a string
        `type` and `properties`; anything else is regenerated as all-required."""
        assert tools, "no tools mounted — fixture is wrong, not the code"
        for name, t in tools.items():
            schema = t.input_schema
            assert isinstance(schema.get("type"), str), name
            assert "properties" in schema, name
            assert "required" in schema, name

    def test_required_is_a_subset_of_properties(self, tools: Dict[str, Any]) -> None:
        for name, t in tools.items():
            extra = set(t.input_schema["required"]) - set(t.input_schema["properties"])
            assert not extra, f"{name} requires undeclared params: {extra}"

    def test_multi_param_tools_have_something_optional(
        self, tools: Dict[str, Any]
    ) -> None:
        """A tool whose every param is required is the bug restated.

        Exempt: tools whose params are ALL pure addressing — an id the model
        looked up plus the replacement text it was given. Nothing there can be
        invented, so requiring all of it forces no fabrication.
        """
        addressing_only = {"memory_correct"}
        for name, t in tools.items():
            props = t.input_schema["properties"]
            if len(props) < 2 or name in addressing_only:
                continue
            optional = set(props) - set(t.input_schema["required"])
            assert optional, (
                f"{name} marks every one of its {len(props)} params required — "
                "the model must invent the ones it wasn't told"
            )


class TestRequiredMatchesHandler:
    """Required must mean 'the handler rejects the call without it'."""

    async def test_task_add_without_due_succeeds_and_stores_no_due_date(
        self, tools: Dict[str, Any], task_repo: _StubTaskRepo
    ) -> None:
        assert "due" not in tools["task_add"].input_schema["required"]
        result = await tools["task_add"].handler({"title": "ship dv-6f2a"})
        assert not result.get("isError"), result
        assert task_repo.calls[0]["due_at"] is None

    async def test_memory_list_needs_no_arguments(self, tools: Dict[str, Any]) -> None:
        assert tools["memory_list"].input_schema["required"] == []

    async def test_remember_requires_only_the_fact(self, tools: Dict[str, Any]) -> None:
        assert tools["remember"].input_schema["required"] == ["fact"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "morning", "prompt": "hi", "cron": "0 7 * * *"},
            {"name": "once", "prompt": "hi", "at": "2099-01-01T09:00:00+00:00"},
        ],
        ids=["cron-only", "at-only"],
    )
    async def test_schedule_add_accepts_exactly_one_of_cron_at(
        self, tools: Dict[str, Any], payload: Dict[str, Any]
    ) -> None:
        """The impossible contract: the old schema required BOTH cron and at,
        while the handler rejects the call when both are given."""
        for key in ("cron", "at"):
            assert key not in tools["schedule_add"].input_schema["required"]
        result = await tools["schedule_add"].handler(payload)
        assert not result.get("isError"), result

    async def test_schedule_add_still_rejects_both(self, tools: Dict[str, Any]) -> None:
        result = await tools["schedule_add"].handler(
            {
                "name": "n",
                "prompt": "p",
                "cron": "0 7 * * *",
                "at": "2099-01-01T09:00:00+00:00",
            }
        )
        assert result.get("isError")
        assert "exactly one" in result["content"][0]["text"]

    async def test_schedule_add_still_rejects_neither(
        self, tools: Dict[str, Any]
    ) -> None:
        result = await tools["schedule_add"].handler({"name": "n", "prompt": "p"})
        assert result.get("isError")
        assert "exactly one" in result["content"][0]["text"]


class TestToolSchemaHelper:
    def test_required_names_must_be_declared(self) -> None:
        with pytest.raises(ValueError, match="undeclared"):
            tool_schema({"a": str}, "b")

    def test_empty_schema_requires_nothing(self) -> None:
        assert tool_schema({}) == {
            "type": "object",
            "properties": {},
            "required": [],
        }


class TestNoShorthandAnywhere:
    """The repo-wide guard — the part that closes the class.

    dv-9679 fixed user_data_tools.py; agent_tools.py carried the identical bug
    for weeks because nothing checked the other files. A per-file acceptance
    criterion cannot prevent the third occurrence; this can.
    """

    def test_no_tool_declaration_uses_the_dict_shorthand(self) -> None:
        offenders: List[str] = []
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not our file to fix
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "tool" or len(node.args) < 3:
                    continue
                if isinstance(node.args[2], ast.Dict):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            "@tool(...) called with a bare dict schema at "
            + ", ".join(offenders)
            + " — the SDK marks every key required (dv-9679/dv-6f2a). "
            "Use tool_schema({...}, *required) from src/claude/tool_schema.py."
        )
