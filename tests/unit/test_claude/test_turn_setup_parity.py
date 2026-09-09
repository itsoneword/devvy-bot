"""Both backends set a turn up identically (dv-a342).

`src/claude/turn_setup.py` is the one place that resolves a turn's identity,
composes its persona text and computes the telegram image roots. The Claude
SDK manager and the Codex CLI backend both call it. These tests pin the
parity — same inputs, same TurnContext, same composed text, same roots — and
the one deliberate difference: Claude passes the text as ``system_prompt``,
Codex prepends it to the user prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, CodexSessionStore
from src.bot.utils.image_extractor import IMAGE_ROOTS_ENV
from src.claude.prompt_composer import PromptComposer
from src.claude.sdk_integration import ClaudeSDKManager
from src.claude.tool_context import TurnContext
from src.claude.turn_setup import (
    compose_persona,
    image_roots_env,
    resolve_turn_context,
)
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry

USER_ID = 111
FAMILY_ID = 7
CHAT_ID = 555


class _FakeRetriever:
    """Records the scopes it was asked for; returns fixed facts."""

    def __init__(self) -> None:
        self.selects: List[Tuple[str, str]] = []
        self.family_selects: List[Tuple[str, str]] = []

    async def select(
        self, agent_id: str, query: str = ""
    ) -> Tuple[List[str], List[str], List[str]]:
        self.selects.append((agent_id, query))
        return (["[01.09.26] never salt"], ["[02.09.26] likes basil"], ["ptr-1"])

    async def select_family(self, agent_id: str, query: str = "") -> List[str]:
        self.family_selects.append((agent_id, query))
        return ["[03.09.26] family eats at 19:00"]


@dataclass
class _Bundle:
    text: str


class _FakeBundleProvider:
    def __init__(self, text: Optional[str]) -> None:
        self.text = text
        self.calls: List[Dict[str, Any]] = []

    async def get_or_materialize(self, **kwargs: Any) -> Optional[_Bundle]:
        self.calls.append(kwargs)
        return _Bundle(self.text) if self.text is not None else None


def _registry(tmp_path: Path) -> ProfileRegistry:
    coach = tmp_path / "coach"
    coach.mkdir()
    (coach / "agent.md").write_text("You are the coach.", encoding="utf-8")
    devops = tmp_path / "devops"
    devops.mkdir()
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="Coach",
                working_dir=coach,
                memory_dir=coach / "memory",
                persona=coach / "agent.md",
                personal_memory=True,
            ),
            ProfileDefinition(
                slug="devops",
                name="Devops",
                working_dir=devops,
                memory_dir=devops / "memory",
                persona=devops / "agent.md",
                default=True,
            ),
        ]
    )


def _settings(tmp_path: Path, mcp_servers: Optional[dict] = None) -> Settings:
    kwargs: Dict[str, Any] = dict(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        enable_mcp=False,
    )
    if mcp_servers is not None:
        config = tmp_path / "mcp.json"
        config.write_text(json.dumps({"mcpServers": mcp_servers}))
        kwargs.update(enable_mcp=True, mcp_config_path=config)
    return Settings(**kwargs)


@pytest.fixture
def pair(tmp_path: Path) -> Tuple[ClaudeSDKManager, CodexCliIntegration, Any]:
    registry = _registry(tmp_path)
    settings = _settings(tmp_path)
    retriever = _FakeRetriever()
    claude = ClaudeSDKManager(
        settings, profile_registry=registry, memory_retriever=retriever
    )
    codex = CodexCliIntegration(
        config=settings,
        session_store=CodexSessionStore(tmp_path / "s.json"),
        profile_registry=registry,
        memory_retriever=retriever,
    )
    return claude, codex, retriever


class TestTurnContextParity:
    def test_both_backends_resolve_the_same_context(self, pair, tmp_path) -> None:
        claude, codex, _ = pair
        coach = tmp_path / "coach"

        from_claude = claude._turn_context_for(
            coach,
            user_id=USER_ID,
            requester_chat_id=CHAT_ID,
            family_id=FAMILY_ID,
            role="owner",
        )
        from_codex = codex._turn_context(coach, USER_ID, CHAT_ID, FAMILY_ID, "owner")

        assert from_claude == from_codex
        assert from_claude == TurnContext(
            profile_slug="coach",
            working_dir=coach,
            requester_chat_id=CHAT_ID,
            memory_agent_id=f"coach#u{USER_ID}",
            family_agent_id=f"f{FAMILY_ID}:coach",
            user_id=USER_ID,
            role="owner",
        )

    def test_family_scope_only_with_a_family(self, tmp_path) -> None:
        registry = _registry(tmp_path)
        ctx = resolve_turn_context(registry, tmp_path / "coach", user_id=USER_ID)
        assert ctx.family_agent_id is None
        assert ctx.role is None  # fail closed (dv-a900)

    def test_no_registry_falls_back_to_the_default_scope(self, tmp_path) -> None:
        ctx = resolve_turn_context(None, tmp_path, user_id=USER_ID, family_id=3)
        assert ctx.profile_slug == "devops"
        assert ctx.memory_agent_id == "devops"
        assert ctx.family_agent_id == "f3:devops"

    def test_the_sdk_agent_server_is_built_from_the_shared_context(
        self, pair, tmp_path, monkeypatch
    ) -> None:
        """`_agent_mcp_server_for` must hand the builder exactly the shared
        resolution, not a private re-derivation."""
        from src.claude import agent_tools

        claude, codex, _ = pair
        captured: List[Dict[str, Any]] = []

        def fake_build(*args: Any, **kwargs: Any) -> Dict[str, str]:
            captured.append(kwargs)
            return {"stub": "server"}

        monkeypatch.setattr(agent_tools, "build_agent_mcp_server", fake_build)
        claude.attach_agent_services(
            agent_tools.AgentServices(event_bus=object(), task_repo=object())
        )
        coach = tmp_path / "coach"
        claude._agent_mcp_server_for(
            coach,
            requester_chat_id=CHAT_ID,
            user_id=USER_ID,
            family_id=FAMILY_ID,
            role="guest",
        )
        ctx = codex._turn_context(coach, USER_ID, CHAT_ID, FAMILY_ID, "guest")
        (kwargs,) = captured
        assert kwargs == {
            "requester_chat_id": ctx.requester_chat_id,
            "memory_agent_id": ctx.memory_agent_id,
            "memory_family_agent_id": ctx.family_agent_id,
            "user_id": ctx.user_id,
            "role": ctx.role,
        }


class TestPersonaParity:
    async def test_live_compose_is_identical_on_both_backends(
        self, pair, tmp_path
    ) -> None:
        claude, codex, retriever = pair
        coach = tmp_path / "coach"

        from_claude = await claude._persona_text(
            coach, "what do I eat", user_id=USER_ID, family_id=FAMILY_ID
        )
        from_codex = await codex._persona_text(
            coach, "what do I eat", user_id=USER_ID, family_id=FAMILY_ID
        )

        assert from_claude is not None
        assert from_claude == from_codex
        assert "You are the coach." in from_claude
        assert "never salt" in from_claude
        assert "likes basil" in from_claude
        assert "family eats at 19:00" in from_claude
        # Both backends asked the retriever for the same scopes, with the
        # user's prompt as the query.
        scope = f"coach#u{USER_ID}"
        assert retriever.selects == [(scope, "what do I eat")] * 2
        assert (
            retriever.family_selects == [(f"f{FAMILY_ID}:coach", "what do I eat")] * 2
        )

    async def test_only_the_destination_differs(self, pair, tmp_path) -> None:
        """Claude: system_prompt. Codex: prepended to the user prompt."""
        claude, codex, _ = pair
        coach = tmp_path / "coach"

        text = await claude._persona_text(coach, "hi", user_id=USER_ID)
        wrapped = await codex._with_persona(coach, "hi", user_id=USER_ID)

        assert wrapped == f"{text}\n\n<user_request>\nhi\n</user_request>"

    async def test_frozen_bundle_wins_on_both_backends(self, pair, tmp_path) -> None:
        """dv-5e2a: the same window must inject the same frozen text on
        either backend, or the bundle un-freezes on a backend switch."""
        claude, codex, retriever = pair
        coach = tmp_path / "coach"
        provider = _FakeBundleProvider("FROZEN BUNDLE")
        claude.bundle_provider = provider  # type: ignore[assignment]
        codex.bundle_provider = provider  # type: ignore[assignment]

        from_claude = await claude._persona_text(
            coach, "hi", user_id=USER_ID, family_id=FAMILY_ID
        )
        from_codex = await codex._persona_text(
            coach, "hi", user_id=USER_ID, family_id=FAMILY_ID
        )

        assert from_claude == from_codex == "FROZEN BUNDLE"
        assert retriever.selects == []  # no live compose behind a bundle
        assert [c["family_id"] for c in provider.calls] == [FAMILY_ID, FAMILY_ID]

    async def test_bundle_needs_a_user_id_to_key_the_window(
        self, pair, tmp_path
    ) -> None:
        claude, codex, retriever = pair
        coach = tmp_path / "coach"
        provider = _FakeBundleProvider("FROZEN BUNDLE")
        claude.bundle_provider = provider  # type: ignore[assignment]
        codex.bundle_provider = provider  # type: ignore[assignment]

        from_claude = await claude._persona_text(coach, "hi")
        from_codex = await codex._persona_text(coach, "hi")

        assert provider.calls == []
        assert from_claude == from_codex
        assert "You are the coach." in (from_claude or "")
        assert retriever.selects == [("coach", "hi")] * 2

    async def test_missing_bundle_falls_through_to_live_compose(
        self, pair, tmp_path
    ) -> None:
        claude, codex, retriever = pair
        coach = tmp_path / "coach"
        provider = _FakeBundleProvider(None)
        claude.bundle_provider = provider  # type: ignore[assignment]
        codex.bundle_provider = provider  # type: ignore[assignment]

        from_claude = await claude._persona_text(coach, "hi", user_id=USER_ID)
        from_codex = await codex._persona_text(coach, "hi", user_id=USER_ID)

        assert from_claude == from_codex
        assert "You are the coach." in (from_claude or "")
        assert len(retriever.selects) == 2

    async def test_full_access_reaches_the_composer_and_the_bundle(
        self, pair, tmp_path
    ) -> None:
        """The SDK path threads its sandbox flag through the shared call."""
        claude, _, _ = pair
        coach = tmp_path / "coach"

        confined = await claude._persona_text(coach, "hi", user_id=USER_ID)
        free = await claude._persona_text(
            coach, "hi", user_id=USER_ID, full_access=True
        )
        assert confined is not None and free is not None
        assert "must stay within" in confined
        assert "full-access to the server" in free

        provider = _FakeBundleProvider("B")
        claude.bundle_provider = provider  # type: ignore[assignment]
        await claude._persona_text(coach, "hi", user_id=USER_ID, full_access=True)
        assert provider.calls[-1]["full_access"] is True

    async def test_no_composer_means_none_and_each_backend_decides(
        self, tmp_path
    ) -> None:
        settings = _settings(tmp_path)
        claude = ClaudeSDKManager(settings)
        codex = CodexCliIntegration(
            config=settings, session_store=CodexSessionStore(tmp_path / "s.json")
        )

        assert await claude._persona_text(tmp_path, "hi", user_id=USER_ID) is None
        assert await codex._with_persona(tmp_path, "hi", user_id=USER_ID) == "hi"

    async def test_codex_swallows_an_unreadable_persona(self, tmp_path) -> None:
        """Codex keeps its recovery: an OSError from compose sends the bare
        prompt rather than failing the turn."""

        class _Broken(PromptComposer):
            def compose(self, *args: Any, **kwargs: Any) -> str:
                raise OSError("persona unreadable")

        settings = _settings(tmp_path)
        composer = _Broken(memory_path=tmp_path, agent_md_path=tmp_path / "a.md")
        codex = CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            prompt_composer=composer,
        )
        assert await codex._with_persona(tmp_path, "hi", user_id=USER_ID) == "hi"

        ctx = resolve_turn_context(None, tmp_path, user_id=USER_ID)
        with pytest.raises(OSError):
            await compose_persona(
                ctx,
                prompt="hi",
                composer=composer,
                memory_retriever=None,
                bundle_provider=None,
            )


class TestImageRootsParity:
    def test_both_transports_tell_the_telegram_server_the_same_roots(
        self, tmp_path
    ) -> None:
        settings = _settings(
            tmp_path, {"telegram": {"command": "/usr/bin/python3", "args": ["tg.py"]}}
        )
        registry = _registry(tmp_path)
        claude = ClaudeSDKManager(settings, profile_registry=registry)
        codex = CodexCliIntegration(
            config=settings,
            session_store=CodexSessionStore(tmp_path / "s.json"),
            profile_registry=registry,
        )
        coach = tmp_path / "coach"

        servers: Dict[str, Any] = claude._load_mcp_config(settings.mcp_config_path)
        claude._inject_image_roots(servers, coach)
        sdk_roots = servers["telegram"]["env"][IMAGE_ROOTS_ENV]

        overrides = codex._stdio_mcp_overrides(coach)
        prefix = f"mcp_servers.telegram.env.{IMAGE_ROOTS_ENV}="
        (codex_roots,) = [
            json.loads(arg[len(prefix) :])
            for arg in overrides
            if arg.startswith(prefix)
        ]

        assert sdk_roots == codex_roots == image_roots_env(tmp_path, coach)
        assert str(tmp_path) in sdk_roots

    def test_working_dir_is_the_fallback_root(self, tmp_path) -> None:
        assert str(tmp_path / "wd") in image_roots_env(None, tmp_path / "wd")
        assert str(tmp_path / "wd") not in image_roots_env(tmp_path, tmp_path / "wd")
