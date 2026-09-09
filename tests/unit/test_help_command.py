"""D5a: /help is the contract that makes the short menu safe.

Unlisting eleven working commands is only acceptable because one place lists
them. These tests pin the two properties that make that true: /help is
DERIVED from COMMAND_TABLE (so a command added later cannot go missing), and
it is role-filtered by OWNER_ONLY_COMMANDS and profile.allows_role — never by
a hand-written list.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import (
    COMMAND_TABLE,
    OWNER_ONLY_COMMANDS,
    MessageOrchestrator,
)
from src.config import create_test_config


def _profile(slug: str, name: str, roles: set) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name=name,
        working_dir=Path("/tmp") / slug,
        allows_role=lambda role, _roles=roles: role in _roles,
    )


def _registry() -> SimpleNamespace:
    profiles = [
        _profile("devops", "DevOps", {"owner"}),
        _profile("general", "General", {"owner", "guest"}),
    ]
    by_slug = lambda slug: next(
        (p for p in profiles if p.slug == slug), None
    )  # noqa: E731
    return SimpleNamespace(
        profiles=profiles,
        enabled_profiles=profiles,
        get_by_slug=by_slug,
        get_enabled_by_slug=by_slug,
        get_default=lambda: profiles[0],
    )


def _orchestrator(tmp_path, registry=None) -> MessageOrchestrator:
    settings = create_test_config(approved_directory=str(tmp_path))
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "profile_registry": registry,
    }
    return MessageOrchestrator(settings, deps)


async def _help_text(orchestrator, role: str, registry=None) -> str:
    update = MagicMock()
    update.effective_user.id = 42
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"role": role}
    context.bot_data = {"profile_registry": registry}

    await orchestrator.agentic_help(update, context)
    return update.message.reply_text.call_args[0][0]


async def test_owner_help_names_every_command_the_owner_can_run(tmp_path):
    """Including the eleven that are not in the menu."""
    registry = _registry()
    orchestrator = _orchestrator(tmp_path, registry)
    text = await _help_text(orchestrator, "owner", registry)

    listed = {c.command for c in await orchestrator.get_bot_commands()}
    registered = {spec.name for spec in orchestrator._available_commands()}
    assert registered - listed, "this test is pointless if nothing is unlisted"

    for name in registered:
        assert f"/{name} " in text or f"/{name} —" in text, name


async def test_owner_help_includes_the_profile_switch_commands(tmp_path):
    registry = _registry()
    orchestrator = _orchestrator(tmp_path, registry)
    text = await _help_text(orchestrator, "owner", registry)

    assert "/devops" in text
    assert "/general" in text


async def test_guest_help_advertises_no_owner_only_command(tmp_path):
    registry = _registry()
    orchestrator = _orchestrator(tmp_path, registry)
    text = await _help_text(orchestrator, "guest", registry)

    for name in OWNER_ONLY_COMMANDS:
        assert f"/{name} " not in text and f"/{name} —" not in text, name
    assert "/help" in text
    assert "/usage" in text


async def test_guest_help_hides_profiles_the_guest_may_not_use(tmp_path):
    """profile.allows_role is the authority, not a hand-written list."""
    registry = _registry()
    orchestrator = _orchestrator(tmp_path, registry)
    text = await _help_text(orchestrator, "guest", registry)

    assert "/general" in text
    assert "/devops" not in text


async def test_help_is_derived_not_hardcoded(tmp_path):
    """A command added to the table must appear in help with no other edit."""
    from dataclasses import replace

    registry = _registry()
    orchestrator = _orchestrator(tmp_path, registry)
    invented = replace(
        COMMAND_TABLE[-1], name="frobnicate", description="Frobnicate the thing"
    )

    original = orchestrator._available_commands

    def _with_invented():
        return list(original()) + [invented]

    orchestrator._available_commands = _with_invented  # type: ignore[method-assign]
    text = await _help_text(orchestrator, "owner", registry)

    assert "/frobnicate" in text
    assert "Frobnicate the thing" in text


async def test_help_works_without_a_profile_registry(tmp_path):
    orchestrator = _orchestrator(tmp_path, None)
    text = await _help_text(orchestrator, "owner", None)

    assert "/start" in text
    assert "/devops" not in text
