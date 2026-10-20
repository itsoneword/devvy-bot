"""Tests for guest isolation: workspace, memory scope, media routing, cost cap.

Covers §4, §5, §7 of docs/plans/2026-07-19-multiuser-guest-mvp.md plus the
media-path routing gap fix:
- §4: per-guest working_dir (<profile>/guests/<user_id>) + registry resolution
  of guest subdirs (sandbox / persona / memory scope stay on the profile —
  security-critical: no fallback to the full-access devops default).
- Gap fix: document/photo/voice turns route guests through the same
  profile + guest-subdir logic as text.
- §5: guest-scoped memory agent_id ({slug}#u{user_id}) on read + write paths.
- §7: per-tier spend cap (claude_max_cost_per_user_guest).
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.middleware.rate_limit import rate_limit_middleware
from src.bot.orchestration.routing import ProfileAccessDenied, apply_route
from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    agent_id_for_working_dir,
    guest_agent_id,
    guest_working_dir,
)
from src.security.rate_limiter import RateLimiter
from src.storage.database import DatabaseManager
from src.storage.memory_repository import MemoryRepository

OWNER_ID = 111
GUEST_ID = 222


def _profile(slug, root, default=False, sandbox=None, guest_ok=False):
    roles = frozenset({"owner", "guest"}) if guest_ok else frozenset({"owner"})
    return ProfileDefinition(
        slug=slug,
        name=slug.title(),
        working_dir=Path(root),
        memory_dir=Path(root) / "m",
        persona=Path(root) / "p.md",
        default=default,
        sandbox=sandbox,
        allowed_roles=roles,
        # These tests exercise guest-vs-owner isolation on SHARED profiles
        # (`shared: true` in YAML) — owners deliberately keep the root here.
        # Per-user owner workspaces are covered in test_personal_workspaces.
        personal_memory=False,
    )


@pytest.fixture
def roots(tmp_path):
    return {
        "devops": tmp_path / "dev",
        "general": tmp_path / "gen",
        "chef": tmp_path / "chef",
    }


@pytest.fixture
def registry(roots):
    return ProfileRegistry(
        [
            _profile("devops", roots["devops"], default=True, sandbox=False),
            _profile("general", roots["general"], sandbox=True, guest_ok=True),
            _profile("chef", roots["chef"], sandbox=True, guest_ok=True),
        ]
    )


# ---------------------------------------------------------------------------
# §4 Registry resolution of guest subdirs (security-critical)
# ---------------------------------------------------------------------------


class TestRegistryGuestResolution:
    def test_exact_root_resolves_profile_no_guest(self, registry, roots):
        profile, guest = registry.resolve_working_dir(roots["general"])
        assert profile.slug == "general"
        assert guest is None

    def test_guest_subdir_resolves_owning_profile(self, registry, roots):
        path = roots["general"] / "guests" / str(GUEST_ID)
        profile, guest = registry.resolve_working_dir(path)
        assert profile.slug == "general"
        assert guest == GUEST_ID

    def test_unknown_path_resolves_none(self, registry, tmp_path):
        assert registry.resolve_working_dir(tmp_path / "nope") == (None, None)

    def test_non_numeric_guest_dir_is_not_a_guest_workspace(self, registry, roots):
        path = roots["general"] / "guests" / "alice"
        assert registry.resolve_working_dir(path) == (None, None)

    def test_guest_subdir_of_unknown_root_resolves_none(self, registry, tmp_path):
        path = tmp_path / "other" / "guests" / str(GUEST_ID)
        assert registry.resolve_working_dir(path) == (None, None)

    def test_get_by_working_dir_covers_guest_subdirs(self, registry, roots):
        """Sandbox / persona / MCP resolution all key off get_by_working_dir —
        a guest subdir MUST map to its profile, never fall through to None."""
        path = roots["chef"] / "guests" / str(GUEST_ID)
        assert registry.get_by_working_dir(path).slug == "chef"

    def test_agent_id_profile_root_is_slug(self, registry, roots):
        assert agent_id_for_working_dir(registry, roots["general"]) == "general"

    def test_agent_id_guest_subdir_is_guest_scoped(self, registry, roots):
        path = roots["general"] / "guests" / str(GUEST_ID)
        assert agent_id_for_working_dir(registry, path) == f"general#u{GUEST_ID}"

    def test_agent_id_unknown_path_defaults_devops(self, registry, tmp_path):
        assert agent_id_for_working_dir(registry, tmp_path / "nope") == "devops"

    def test_agent_id_no_registry_defaults_devops(self, roots):
        assert agent_id_for_working_dir(None, roots["general"]) == "devops"

    def test_guest_agent_id_format(self):
        assert guest_agent_id("chef", GUEST_ID) == f"chef#u{GUEST_ID}"

    def test_guest_working_dir_derivation(self, registry, roots):
        profile = registry.get_by_slug("general")
        assert guest_working_dir(profile, GUEST_ID) == (
            roots["general"] / "guests" / str(GUEST_ID)
        )

    def test_sandbox_resolution_for_guest_subdir(self, registry, roots):
        """SECURITY: a guest workspace must inherit its profile's sandbox flag,
        not fall back to the global default (which may be full access)."""
        from src.claude.sdk_integration import ClaudeSDKManager

        config = create_test_config(sandbox_enabled=False)
        mgr = ClaudeSDKManager(config, profile_registry=registry)
        guest_dir = roots["general"] / "guests" / str(GUEST_ID)
        # general is sandbox=True; global default False must NOT win.
        assert mgr._sandbox_enabled_for(guest_dir) is True
        # And the memory scope is the guest-scoped id, not devops.
        assert mgr._agent_id_for(guest_dir) == f"general#u{GUEST_ID}"

    def test_composer_for_guest_subdir_uses_profile_persona(self, registry, roots):
        """A guest media/text turn must compose the profile persona, not the
        default (devops/full-access) composer."""
        from src.claude.sdk_integration import ClaudeSDKManager

        config = create_test_config(sandbox_enabled=False)
        default_composer = MagicMock()
        mgr = ClaudeSDKManager(
            config, prompt_composer=default_composer, profile_registry=registry
        )
        guest_dir = roots["general"] / "guests" / str(GUEST_ID)
        composer = mgr._composer_for(guest_dir)
        assert composer is not default_composer
        assert composer is mgr._composer_for(roots["general"])


# ---------------------------------------------------------------------------
# §4 apply_route: per-guest working dir
# ---------------------------------------------------------------------------


class TestApplyRouteGuestWorkspace:
    def test_owner_keeps_profile_root(self, registry, roots):
        user_data = {"active_profile": "general"}
        decision = apply_route(
            "hi", user_data, registry, role="owner", user_id=OWNER_ID
        )
        assert decision.working_dir == roots["general"]
        assert not (roots["general"] / "guests").exists()

    def test_guest_gets_guest_subdir_and_it_is_created(self, registry, roots):
        user_data = {"active_profile": "general"}
        decision = apply_route(
            "hi", user_data, registry, role="guest", user_id=GUEST_ID
        )
        expected = roots["general"] / "guests" / str(GUEST_ID)
        assert decision.working_dir == expected
        assert expected.is_dir()
        assert user_data["current_directory"] == expected

    def test_guest_default_profile_guest_subdir(self, registry, roots):
        """No active profile: guest lands in general's guest subdir, never the
        registry default (full-access devops)."""
        user_data: dict = {}
        decision = apply_route(
            "hi", user_data, registry, role="guest", user_id=GUEST_ID
        )
        assert decision.working_dir == roots["general"] / "guests" / str(GUEST_ID)

    def test_guest_one_shot_targets_guest_subdir(self, registry, roots):
        user_data = {"active_profile": "general"}
        decision = apply_route(
            "@chef dinner?", user_data, registry, role="guest", user_id=GUEST_ID
        )
        assert decision.working_dir == roots["chef"] / "guests" / str(GUEST_ID)
        assert user_data["active_profile"] == "general"

    def test_guest_without_user_id_keeps_root(self, registry, roots):
        """Defensive back-compat: no user id -> cannot scope, keep root."""
        decision = apply_route("hi", {}, registry, role="guest")
        assert decision.working_dir == roots["general"]

    def test_guest_round_trips_through_agent_id(self, registry, roots):
        """The routed guest dir maps back to the guest-scoped memory id."""
        decision = apply_route("hi", {}, registry, role="guest", user_id=GUEST_ID)
        assert agent_id_for_working_dir(registry, decision.working_dir) == (
            f"general#u{GUEST_ID}"
        )


# ---------------------------------------------------------------------------
# Media-path routing (gap fix) + switch handlers
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator():
    with tempfile.TemporaryDirectory() as d:
        settings = create_test_config(approved_directory=d, agentic_mode=True)
        deps = {
            "claude_integration": MagicMock(),
            "storage": MagicMock(),
            "security_validator": MagicMock(),
            "rate_limiter": MagicMock(),
            "audit_logger": MagicMock(),
        }
        yield MessageOrchestrator(settings, deps)


def _context(role, registry=None, user_data=None):
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {"role": role}
    context.user_data.setdefault("role", role)
    auth_manager = MagicMock()
    auth_manager.get_user_role = MagicMock(return_value=role)
    storage = MagicMock()
    storage.log_bot_event = AsyncMock()
    context.bot_data = {
        "auth_manager": auth_manager,
        "storage": storage,
        "profile_registry": registry,
    }
    return context


class TestMediaWorkingDir:
    def test_owner_routes_through_default_profile(self, orchestrator, registry, roots):
        context = _context("owner", registry, {"current_directory": roots["devops"]})
        assert orchestrator._media_working_dir(context, OWNER_ID) == roots["devops"]

    def test_owner_post_restart_media_routes_to_active_profile(
        self, orchestrator, registry, roots
    ):
        """dv-b3d7 regression: after a restart ``current_directory`` is gone
        (not a persisted pref) but ``active_profile`` is DB-seeded. The old
        owner short-circuit fell back to the home dir -> default profile;
        the media turn must land in the active profile's workspace."""
        context = _context("owner", registry, {"active_profile": "chef"})
        assert orchestrator._media_working_dir(context, OWNER_ID) == roots["chef"]

    def test_owner_stale_current_directory_is_overridden(
        self, orchestrator, registry, roots
    ):
        context = _context(
            "owner",
            registry,
            {"active_profile": "chef", "current_directory": roots["devops"]},
        )
        assert orchestrator._media_working_dir(context, OWNER_ID) == roots["chef"]

    def test_owner_media_matches_text_routing(self, orchestrator, registry, roots):
        """dv-b3d7 criterion: text and media turns in the same chat resolve
        to the same profile workspace."""
        text_dir = apply_route(
            "hi",
            {"active_profile": "chef"},
            registry,
            role="owner",
            user_id=OWNER_ID,
        ).working_dir
        context = _context("owner", registry, {"active_profile": "chef"})
        assert orchestrator._media_working_dir(context, OWNER_ID) == text_dir

    def test_guest_routed_to_active_profile_guest_subdir(
        self, orchestrator, registry, roots
    ):
        context = _context("guest", registry, {"active_profile": "chef"})
        result = orchestrator._media_working_dir(context, GUEST_ID)
        assert result == roots["chef"] / "guests" / str(GUEST_ID)
        assert result.is_dir()

    def test_guest_first_media_message_defaults_to_general(
        self, orchestrator, registry, roots
    ):
        """The original bug: a guest's first media turn fell back to the home
        dir (-> devops full-access prompt composition). Must land in the
        general guest workspace instead."""
        context = _context("guest", registry, {})
        result = orchestrator._media_working_dir(context, GUEST_ID)
        assert result == roots["general"] / "guests" / str(GUEST_ID)

    def test_guest_stale_current_directory_is_overridden(
        self, orchestrator, registry, roots
    ):
        context = _context("guest", registry, {"current_directory": roots["devops"]})
        result = orchestrator._media_working_dir(context, GUEST_ID)
        assert result == roots["general"] / "guests" / str(GUEST_ID)

    def test_guest_no_allowed_profiles_raises(self, orchestrator, roots):
        registry = ProfileRegistry(
            [_profile("devops", roots["devops"], default=True, sandbox=False)]
        )
        context = _context("guest", registry, {})
        with pytest.raises(ProfileAccessDenied):
            orchestrator._media_working_dir(context, GUEST_ID)

    def test_no_registry_keeps_legacy_behavior(self, orchestrator, roots):
        context = _context("owner", None, {"current_directory": roots["devops"]})
        assert orchestrator._media_working_dir(context, OWNER_ID) == roots["devops"]


class TestMediaUploadDir:
    """dv-9ebe: inbound photos/documents must not land in a shared root.

    `chef` here is shared (`personal_memory=False`) AND guest-reachable — the
    exact shape that leaked: an owner's chef photo was written to the profile
    root, which every guest allowed on chef can read.
    """

    def test_owner_upload_leaves_the_shared_root(self, orchestrator, registry, roots):
        context = _context("owner", registry, {"active_profile": "chef"})
        upload = orchestrator._media_upload_dir(context, OWNER_ID)
        assert upload == roots["chef"] / "users" / str(OWNER_ID) / "uploads"
        assert upload.parent != roots["chef"]

    def test_guest_upload_lands_in_their_own_guest_dir(
        self, orchestrator, registry, roots
    ):
        context = _context("guest", registry, {"active_profile": "chef"})
        upload = orchestrator._media_upload_dir(context, GUEST_ID)
        assert upload == roots["chef"] / "guests" / str(GUEST_ID) / "uploads"

    def test_owner_and_guest_uploads_are_disjoint(self, orchestrator, registry, roots):
        owner_ctx = _context("owner", registry, {"active_profile": "chef"})
        guest_ctx = _context("guest", registry, {"active_profile": "chef"})
        owner_dir = orchestrator._media_upload_dir(owner_ctx, OWNER_ID)
        guest_dir = orchestrator._media_upload_dir(guest_ctx, GUEST_ID)
        assert not owner_dir.is_relative_to(guest_dir)
        assert not guest_dir.is_relative_to(owner_dir)

    def test_upload_stays_inside_the_turn_working_dir(
        self, orchestrator, registry, roots
    ):
        """allowed_image_roots() is built from the turn's working dir — an
        upload outside it could not be delivered back to the user."""
        for role, uid in (("owner", OWNER_ID), ("guest", GUEST_ID)):
            context = _context(role, registry, {"active_profile": "chef"})
            working = orchestrator._media_working_dir(context, uid)
            upload = orchestrator._media_upload_dir(context, uid)
            assert upload.is_relative_to(working) or working.is_relative_to(
                roots["chef"]
            )
            assert upload.is_relative_to(roots["chef"])

    def test_no_registry_falls_back_to_uploads_under_cwd(self, orchestrator, roots):
        context = _context("owner", None, {"current_directory": roots["devops"]})
        assert orchestrator._media_upload_dir(context, OWNER_ID) == (
            roots["devops"] / "uploads"
        )


class TestSwitchHandlerGuestWorkspace:
    async def test_guest_switch_sets_guest_subdir(self, orchestrator, registry, roots):
        orchestrator._save_user_prefs = AsyncMock()
        handler = orchestrator._make_switch_handler("chef")
        update = MagicMock()
        update.effective_user.id = GUEST_ID
        update.message.text = "/chef"
        update.message.reply_text = AsyncMock()
        context = _context("guest", registry, {})

        await handler(update, context)

        expected = roots["chef"] / "guests" / str(GUEST_ID)
        assert context.user_data["current_directory"] == expected
        assert expected.is_dir()

    async def test_owner_switch_keeps_root(self, orchestrator, registry, roots):
        orchestrator._save_user_prefs = AsyncMock()
        handler = orchestrator._make_switch_handler("chef")
        update = MagicMock()
        update.effective_user.id = OWNER_ID
        update.message.text = "/chef"
        update.message.reply_text = AsyncMock()
        context = _context("owner", registry, {})

        await handler(update, context)

        assert context.user_data["current_directory"] == roots["chef"]

    async def test_guest_agents_picker_sets_guest_subdir(
        self, orchestrator, registry, roots
    ):
        orchestrator._save_user_prefs = AsyncMock()
        context = _context("guest", registry, {})
        context.bot_data["audit_logger"] = None
        query = MagicMock()
        query.from_user.id = GUEST_ID
        query.edit_message_text = AsyncMock()

        await orchestrator._handle_agent_callback(query, context, "agent:chef")

        expected = roots["chef"] / "guests" / str(GUEST_ID)
        assert context.user_data["current_directory"] == expected


# ---------------------------------------------------------------------------
# §5 Guest memory isolation
# ---------------------------------------------------------------------------


class TestGuestMemoryIsolation:
    def test_orchestrator_agent_id_guest_scoped(self, orchestrator, registry, roots):
        """Evaluator (fact-writer) path uses the same guest-scoped id."""
        context = _context("guest", registry, {})
        guest_dir = roots["general"] / "guests" / str(GUEST_ID)
        assert orchestrator._agent_id_for(context, guest_dir) == (
            f"general#u{GUEST_ID}"
        )

    async def test_repo_facts_private_per_guest(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "mem.db"))
        await db.initialize()
        try:
            repo = MemoryRepository(db)
            await repo.add_fact("general", "general", "owner-tier fact")
            await repo.add_fact(f"general#u{GUEST_ID}", "general", "guest private fact")

            owner_facts = await repo.get_facts("general")
            guest_facts = await repo.get_facts(f"general#u{GUEST_ID}")

            assert [f.content for f in owner_facts] == ["owner-tier fact"]
            assert [f.content for f in guest_facts] == ["guest private fact"]
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# §7 Spend cap per tier
# ---------------------------------------------------------------------------


class TestGuestSpendCap:
    def test_setting_default_and_env_parse(self):
        assert create_test_config().claude_max_cost_per_user_guest == 2.0
        settings = create_test_config(claude_max_cost_per_user_guest="0.5")
        assert settings.claude_max_cost_per_user_guest == 0.5

    def _limiter(self):
        settings = create_test_config(
            claude_max_cost_per_user="10.0",
            claude_max_cost_per_user_guest="2.0",
        )
        return RateLimiter(settings)

    async def test_guest_blocked_at_guest_cap(self):
        limiter = self._limiter()
        allowed, _ = await limiter.check_rate_limit(GUEST_ID, 1.5, role="guest")
        assert allowed
        allowed, message = await limiter.check_rate_limit(GUEST_ID, 1.5, role="guest")
        assert not allowed
        assert "$2.00" in message

    async def test_owner_cap_unchanged(self):
        limiter = self._limiter()
        allowed, _ = await limiter.check_rate_limit(OWNER_ID, 1.5)
        assert allowed
        allowed, _ = await limiter.check_rate_limit(OWNER_ID, 1.5, role="owner")
        assert allowed  # 3.0 total, well under the 10.0 owner cap

    async def test_owner_is_uncapped(self):
        """Owners have no daily cost cap — hitting one just locks the owner
        out of their own bot with no way to self-serve a raise."""
        limiter = self._limiter()
        allowed, message = await limiter.check_rate_limit(OWNER_ID, 11.0)
        assert allowed
        assert message is None

    def test_get_user_status_reports_role_cap(self):
        limiter = self._limiter()
        assert (
            limiter.get_user_status(GUEST_ID, role="guest")["cost_usage"]["limit"]
            == 2.0
        )
        assert limiter.get_user_status(OWNER_ID)["cost_usage"]["limit"] == 10.0

    async def test_middleware_passes_stamped_guest_role(self):
        rate_limiter = MagicMock()
        rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
        handler = AsyncMock()
        event = MagicMock()
        event.effective_user.id = GUEST_ID
        event.effective_message.text = "hello"
        event.effective_message.document = None
        event.effective_message.photo = None
        data = {
            "rate_limiter": rate_limiter,
            "user_data": {"role": "guest"},
        }

        await rate_limit_middleware(handler, event, data)

        handler.assert_awaited_once()
        assert rate_limiter.check_rate_limit.await_args.kwargs["role"] == "guest"

    async def test_middleware_prefers_auth_manager_role(self):
        rate_limiter = MagicMock()
        rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
        auth_manager = MagicMock()
        auth_manager.get_user_role = MagicMock(return_value="guest")
        event = MagicMock()
        event.effective_user.id = GUEST_ID
        event.effective_message.text = "hello"
        event.effective_message.document = None
        event.effective_message.photo = None
        data = {
            "rate_limiter": rate_limiter,
            "auth_manager": auth_manager,
            "user_data": {"role": "owner"},  # stale stamp loses to live session
        }

        await rate_limit_middleware(AsyncMock(), event, data)

        assert rate_limiter.check_rate_limit.await_args.kwargs["role"] == "guest"

    async def test_middleware_fails_closed_to_guest_without_role_info(self):
        """dv-09d8: unresolvable role gets the guest (most restrictive) tier."""
        rate_limiter = MagicMock()
        rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
        event = MagicMock()
        event.effective_user.id = OWNER_ID
        event.effective_message.text = "hello"
        event.effective_message.document = None
        event.effective_message.photo = None
        data = {"rate_limiter": rate_limiter, "user_data": {}}

        await rate_limit_middleware(AsyncMock(), event, data)

        assert rate_limiter.check_rate_limit.await_args.kwargs["role"] == "guest"

    async def test_middleware_env_owner_keeps_owner_tier_without_role_info(self):
        """Single-owner installs: ALLOWED_USERS ids keep the owner tier."""
        rate_limiter = MagicMock()
        rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
        event = MagicMock()
        event.effective_user.id = OWNER_ID
        event.effective_message.text = "hello"
        event.effective_message.document = None
        event.effective_message.photo = None
        settings = create_test_config(allowed_users=str(OWNER_ID))
        data = {"rate_limiter": rate_limiter, "user_data": {}, "settings": settings}

        await rate_limit_middleware(AsyncMock(), event, data)

        assert rate_limiter.check_rate_limit.await_args.kwargs["role"] == "owner"
