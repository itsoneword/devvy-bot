"""Commands must resolve "the current conversation's directory" exactly like
a text turn does (dv-c07a, second sighting dv-9eb0).

``user_data`` is in-memory only — after every service restart it is empty, so
any handler that reads ``current_directory`` with an ``approved_directory or
Path.home()`` fallback silently targets the service user's home dir in
denylist mode. ``/new`` then rolled a phantom ``/home/<user>`` window while the
conversation kept accounting against the real per-profile/per-user one: the
counter never reset and the evaluator checkpoint never rebased.

The invariant these tests pin: every such command goes through
``OrchestratorBase._routed_working_dir``, which returns the same path the turn
path computes via ``apply_route(...)`` -> ``ensure_workspace_for(...)``
(``src/bot/orchestration/turns.py``, ``routing.py``).
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.routing import apply_route
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.profiles.registry import ProfileDefinition, ProfileRegistry

USER_ID = 111111111


@pytest.fixture
def denylist_settings(tmp_path):
    """Native-service shape: no approved_directory, so the old fallback was
    ``Path.home()`` — the exact production config that produced the bug."""
    settings = create_test_config(
        approved_directory=str(tmp_path / "unused"), agentic_mode=True
    )
    settings.approved_directory = None
    return settings


@pytest.fixture
def registry(tmp_path):
    """coach (personal_memory -> per-user subdir) + a non-default profile."""
    coach_root = tmp_path / "workspaces" / "coach"
    chef_root = tmp_path / "workspaces" / "chef"
    coach_root.mkdir(parents=True)
    chef_root.mkdir(parents=True)
    return ProfileRegistry(
        [
            ProfileDefinition(
                slug="coach",
                name="Coach",
                working_dir=coach_root,
                memory_dir=coach_root / "memory",
                persona=coach_root / "persona.md",
                default=True,
                personal_memory=True,
            ),
            ProfileDefinition(
                slug="chef",
                name="Chef",
                working_dir=chef_root,
                memory_dir=chef_root / "memory",
                persona=chef_root / "persona.md",
                personal_memory=True,
            ),
        ]
    )


def _window_repo():
    repo = MagicMock()
    state = MagicMock()
    state.last_checkpoint_message_id = 0
    repo.get_or_create = AsyncMock(return_value=state)
    repo.roll_window = AsyncMock()
    repo.set_pending_transition = AsyncMock()
    return repo


def _context(registry, window_repo, user_data, role="owner"):
    """Post-restart shape: ``user_data`` holds nothing but what the caller
    passes; the role comes from the live auth session, as in production."""
    auth_manager = MagicMock()
    auth_manager.get_user_role = MagicMock(return_value=role)
    context = MagicMock()
    context.user_data = user_data
    context.bot_data = {
        "window_repo": window_repo,
        "profile_registry": registry,
        "auth_manager": auth_manager,
        "storage": None,
        "evaluator_service": None,
    }
    return context


def _update():
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.reply_text = AsyncMock()
    return update


def _turn_path_working_dir(registry, user_data, role="owner"):
    """What the text turn pipeline would resolve for this same state.

    Mirrors ``turns.py`` ``resolve_route()``: ``apply_route`` on the live
    ``user_data`` with the resolved role, whose ``working_dir`` comes from
    ``ensure_workspace_for(profile, role, user_id)``. Passed a copy so the
    probe never seeds ``current_directory`` for the code under test.
    """
    return apply_route(
        "hello", dict(user_data), registry, role=role, user_id=USER_ID
    ).working_dir


class TestNewOnAColdProcess:
    async def test_rolls_routed_workspace_not_home(
        self, denylist_settings, registry, tmp_path
    ):
        """Empty user_data (post-restart): /new must roll the profile's
        per-user workspace, never ``Path.home()``."""
        orchestrator = MessageOrchestrator(denylist_settings, {})
        window_repo = _window_repo()
        context = _context(registry, window_repo, {})

        await orchestrator.agentic_new(_update(), context)

        expected = tmp_path / "workspaces" / "coach" / "users" / str(USER_ID)
        rolled_dir = Path(str(window_repo.roll_window.call_args[0][1]))
        assert rolled_dir == expected
        assert rolled_dir != Path.home()
        # The window it evaluated/armed is the same one it rolled.
        assert Path(str(window_repo.get_or_create.call_args[0][1])) == expected
        assert Path(str(window_repo.set_pending_transition.call_args[0][1])) == expected

    async def test_matches_the_turn_path(self, denylist_settings, registry):
        """The dir /new rolls is the dir a text turn would run in — same
        resolver, so the two can never account against different windows."""
        orchestrator = MessageOrchestrator(denylist_settings, {})
        window_repo = _window_repo()
        user_data: dict = {}
        context = _context(registry, window_repo, user_data)

        expected = _turn_path_working_dir(registry, user_data)

        await orchestrator.agentic_new(_update(), context)

        assert Path(str(window_repo.roll_window.call_args[0][1])) == Path(str(expected))

    async def test_follows_the_profile_restored_from_prefs(
        self, denylist_settings, registry, tmp_path
    ):
        """After a restart ``active_profile`` is re-seeded from the DB by
        ``_inject_deps``; /new must honour it rather than the registry
        default, exactly as the turn path does."""
        orchestrator = MessageOrchestrator(denylist_settings, {})
        window_repo = _window_repo()
        user_data = {"active_profile": "chef"}
        context = _context(registry, window_repo, user_data)

        await orchestrator.agentic_new(_update(), context)

        expected = tmp_path / "workspaces" / "chef" / "users" / str(USER_ID)
        assert Path(str(window_repo.roll_window.call_args[0][1])) == expected
        assert Path(str(window_repo.roll_window.call_args[0][1])) == Path(
            str(_turn_path_working_dir(registry, {"active_profile": "chef"}))
        )


class TestOtherCommandsUseTheSameResolver:
    async def test_status_reports_routed_dir(self, denylist_settings, registry):
        orchestrator = MessageOrchestrator(denylist_settings, {})
        context = _context(registry, _window_repo(), {})
        context.bot_data["rate_limiter"] = None
        update = _update()

        await orchestrator.agentic_status(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert str(_turn_path_working_dir(registry, {})) in text

    async def test_start_greets_with_routed_dir(self, denylist_settings, registry):
        orchestrator = MessageOrchestrator(denylist_settings, {})
        context = _context(registry, _window_repo(), {})
        update = _update()
        update.effective_user.first_name = "Owner"

        await orchestrator.agentic_start(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert str(_turn_path_working_dir(registry, {})) in text
        assert f"<code>{Path.home()}/</code>" not in text

    async def test_usage_finds_the_profile_per_request_cap(
        self, denylist_settings, tmp_path
    ):
        """With current_directory unset the lookup used to miss the profile
        entirely and report "no cap" for a capped profile."""
        capped_root = tmp_path / "workspaces" / "capped"
        capped_root.mkdir(parents=True)
        registry = ProfileRegistry(
            [
                ProfileDefinition(
                    slug="coach",
                    name="Coach",
                    working_dir=capped_root,
                    memory_dir=capped_root / "memory",
                    persona=capped_root / "persona.md",
                    default=True,
                    personal_memory=True,
                    max_cost_per_request=0.25,
                )
            ]
        )
        orchestrator = MessageOrchestrator(denylist_settings, {})
        context = _context(registry, _window_repo(), {})
        context.bot_data["rate_limiter"] = None
        context.bot_data["storage"] = None
        update = _update()

        await orchestrator.agentic_usage(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Per-request cap: $0.25" in text

    async def test_memory_reads_the_routed_window(self, denylist_settings, registry):
        """/memory used to read (and ``get_or_create``, i.e. mint) a window row
        for the profile ROOT, while the conversation ran in
        ``<root>/users/<id>``."""
        orchestrator = MessageOrchestrator(denylist_settings, {})
        window_repo = _window_repo()
        window_repo.refresh_threshold_tokens = 100_000
        window_repo.eval_interval_tokens = 20_000
        state = MagicMock()
        state.window_id = 8
        state.last_context_tokens = 1000
        state.last_eval_checkpoint = 0
        state.cumulative_tokens = 1000
        window_repo.get_or_create = AsyncMock(return_value=state)

        composer = MagicMock()
        composer.get_section_sizes.return_value = {"agent.md": 4800}
        context = _context(registry, window_repo, {})
        context.bot_data["prompt_composer"] = composer

        await orchestrator.agentic_memory(_update(), context)

        assert Path(str(window_repo.get_or_create.call_args[0][1])) == Path(
            str(_turn_path_working_dir(registry, {}))
        )
