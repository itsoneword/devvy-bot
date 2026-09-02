"""Tests for the guest tier security core: roles, command gating, profile ACL.

Covers §1-§3 of docs/plans/2026-07-19-multiuser-guest-mvp.md:
- Role primitive (GUEST_USERS setting, provider role resolution, middleware)
- Owner-only command gating (_owner_only wrapper + reduced guest menu)
- Profile ACL (apply_route / switch handler / agents picker + sandbox invariant)
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.middleware.auth import _stamp_role, auth_middleware
from src.bot.middleware.rate_limit import _resolve_role as _resolve_rate_limit_role
from src.bot.middleware.rate_limit import (
    rate_limit_middleware,
)
from src.bot.orchestration.routing import ProfileAccessDenied, apply_route
from src.bot.orchestrator import OWNER_ONLY_COMMANDS, MessageOrchestrator
from src.config import create_test_config
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
)
from src.security.auth import (
    ROLE_GUEST,
    ROLE_OWNER,
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
    fallback_role,
)

OWNER_ID = 111
GUEST_ID = 222
STRANGER_ID = 999


# ---------------------------------------------------------------------------
# §1 Role primitive
# ---------------------------------------------------------------------------


def test_settings_parses_guest_users_like_allowed_users():
    settings = create_test_config(allowed_users="111", guest_users="222, 333")
    assert settings.allowed_users == [111]
    assert settings.guest_users == [222, 333]


def test_settings_guest_users_default_none():
    settings = create_test_config()
    assert settings.guest_users is None


class TestWhitelistRoles:
    def _provider(self):
        return WhitelistAuthProvider([OWNER_ID], guest_users=[GUEST_ID])

    def test_owner_resolves_owner(self):
        assert self._provider().resolve_role(OWNER_ID) == ROLE_OWNER

    def test_guest_resolves_guest(self):
        assert self._provider().resolve_role(GUEST_ID) == ROLE_GUEST

    def test_id_in_both_lists_is_owner(self):
        provider = WhitelistAuthProvider([OWNER_ID], guest_users=[OWNER_ID, GUEST_ID])
        assert provider.resolve_role(OWNER_ID) == ROLE_OWNER
        assert provider.resolve_role(GUEST_ID) == ROLE_GUEST

    def test_stranger_resolves_none(self):
        assert self._provider().resolve_role(STRANGER_ID) is None

    async def test_guest_is_authenticated(self):
        assert await self._provider().authenticate(GUEST_ID, {}) is True

    async def test_stranger_is_rejected(self):
        assert await self._provider().authenticate(STRANGER_ID, {}) is False

    async def test_user_info_carries_role(self):
        provider = self._provider()
        assert (await provider.get_user_info(OWNER_ID))["role"] == ROLE_OWNER
        assert (await provider.get_user_info(GUEST_ID))["role"] == ROLE_GUEST
        assert await provider.get_user_info(STRANGER_ID) is None

    def test_allow_all_dev_is_owner(self):
        provider = WhitelistAuthProvider([], allow_all_dev=True)
        assert provider.resolve_role(STRANGER_ID) == ROLE_OWNER


class TestManagerRoleResolution:
    def _manager(self):
        return AuthenticationManager(
            [WhitelistAuthProvider([OWNER_ID], guest_users=[GUEST_ID])]
        )

    async def test_owner_role(self):
        manager = self._manager()
        assert await manager.authenticate_user(OWNER_ID)
        assert manager.get_user_role(OWNER_ID) == ROLE_OWNER

    async def test_guest_role(self):
        manager = self._manager()
        assert await manager.authenticate_user(GUEST_ID)
        assert manager.get_user_role(GUEST_ID) == ROLE_GUEST

    async def test_unauthenticated_role_is_none(self):
        manager = self._manager()
        assert not await manager.authenticate_user(STRANGER_ID)
        assert manager.get_user_role(STRANGER_ID) is None

    async def test_token_auth_is_owner(self):
        provider = TokenAuthProvider("secret", InMemoryTokenStorage())
        token = await provider.generate_token(GUEST_ID)
        manager = AuthenticationManager([provider])
        assert await manager.authenticate_user(GUEST_ID, {"token": token})
        assert manager.get_user_role(GUEST_ID) == ROLE_OWNER

    async def test_legacy_session_without_role_fails_closed_to_guest(self):
        """dv-09d8: a session with no explicit role never promotes to owner."""
        manager = self._manager()
        assert await manager.authenticate_user(OWNER_ID)
        manager.sessions[OWNER_ID].user_info = {"user_id": OWNER_ID}
        assert manager.get_user_role(OWNER_ID) == ROLE_GUEST


async def test_auth_middleware_stamps_role_into_user_data():
    manager = AuthenticationManager(
        [WhitelistAuthProvider([OWNER_ID], guest_users=[GUEST_ID])]
    )
    handler = AsyncMock()
    event = MagicMock()
    event.effective_user.id = GUEST_ID
    event.effective_user.is_bot = False
    event.effective_message.reply_text = AsyncMock()
    user_data: dict = {}
    data = {"auth_manager": manager, "user_data": user_data}

    await auth_middleware(handler, event, data)

    handler.assert_awaited_once()
    assert user_data["role"] == ROLE_GUEST

    # Second pass (already-authenticated branch) restamps the role.
    user_data.clear()
    await auth_middleware(AsyncMock(), event, data)
    assert user_data["role"] == ROLE_GUEST


# ---------------------------------------------------------------------------
# §1b Fail-closed role fallbacks (dv-09d8)
# ---------------------------------------------------------------------------


def test_fallback_role_defaults_to_guest():
    """Unresolvable identity or missing settings never promote to owner."""
    assert fallback_role(STRANGER_ID, create_test_config()) == ROLE_GUEST
    assert fallback_role(None, create_test_config()) == ROLE_GUEST
    assert fallback_role(STRANGER_ID, None) == ROLE_GUEST


def test_fallback_role_env_owner_stays_owner():
    """Explicit single-owner case: ids in ALLOWED_USERS keep owner."""
    settings = create_test_config(allowed_users=str(OWNER_ID))
    assert fallback_role(OWNER_ID, settings) == ROLE_OWNER
    assert fallback_role(GUEST_ID, settings) == ROLE_GUEST


def test_stamp_role_fails_closed_to_guest():
    """Auth middleware stamp: no resolvable role -> guest, not owner."""
    manager = MagicMock()
    manager.get_user_role = MagicMock(return_value=None)
    user_data: dict = {}
    _stamp_role(manager, STRANGER_ID, {"user_data": user_data})
    assert user_data["role"] == ROLE_GUEST


def test_stamp_role_env_owner_survives_missing_session():
    manager = MagicMock()
    manager.get_user_role = MagicMock(return_value=None)
    settings = create_test_config(allowed_users=str(OWNER_ID))
    user_data: dict = {}
    _stamp_role(manager, OWNER_ID, {"user_data": user_data, "settings": settings})
    assert user_data["role"] == ROLE_OWNER


def test_rate_limit_role_fails_closed_to_guest_tier():
    """Unresolvable role selects the guest (most restrictive) cost cap tier."""
    assert _resolve_rate_limit_role(STRANGER_ID, {}) == ROLE_GUEST


def test_rate_limit_role_env_owner_keeps_owner_tier():
    settings = create_test_config(allowed_users=str(OWNER_ID))
    assert _resolve_rate_limit_role(OWNER_ID, {"settings": settings}) == ROLE_OWNER


def test_rate_limit_role_prefers_session_then_stamp():
    manager = MagicMock()
    manager.get_user_role = MagicMock(return_value=ROLE_OWNER)
    assert _resolve_rate_limit_role(GUEST_ID, {"auth_manager": manager}) == ROLE_OWNER
    assert (
        _resolve_rate_limit_role(GUEST_ID, {"user_data": {"role": ROLE_GUEST}})
        == ROLE_GUEST
    )


async def test_rate_limit_middleware_passes_guest_role_when_unresolvable():
    """The middleware site itself hands the limiter the guest tier."""
    rate_limiter = MagicMock()
    rate_limiter.check_rate_limit = AsyncMock(return_value=(True, None))
    handler = AsyncMock()
    event = MagicMock()
    event.effective_user.id = STRANGER_ID
    event.effective_user.is_bot = False
    event.effective_message.text = "hello"
    event.callback_query = None
    data = {"rate_limiter": rate_limiter}

    await rate_limit_middleware(handler, event, data)

    handler.assert_awaited_once()
    assert rate_limiter.check_rate_limit.await_args.kwargs["role"] == ROLE_GUEST


# ---------------------------------------------------------------------------
# §2 Owner-only command gating
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def orchestrator(tmp_dir):
    settings = create_test_config(approved_directory=str(tmp_dir), agentic_mode=True)
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


def _context(role, *, with_auth_manager=True):
    context = MagicMock()
    context.user_data = {"role": role}
    context.bot_data = {"storage": None}
    if with_auth_manager:
        auth_manager = MagicMock()
        auth_manager.get_user_role = MagicMock(return_value=role)
        context.bot_data["auth_manager"] = auth_manager
    storage = MagicMock()
    storage.log_bot_event = AsyncMock()
    context.bot_data["storage"] = storage
    return context


def _update(user_id=GUEST_ID):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    update.message.reply_text = AsyncMock()
    return update


async def test_owner_only_blocks_guest_and_logs(orchestrator):
    inner = AsyncMock()
    wrapped = orchestrator._owner_only(inner, "restart")
    update = _update()
    context = _context("guest")

    await wrapped(update, context)

    inner.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        "This command is owner-only."
    )
    context.bot_data["storage"].log_bot_event.assert_awaited_once()
    args = context.bot_data["storage"].log_bot_event.await_args
    assert args.args[0] == GUEST_ID
    assert args.args[1] == "owner_only_denied"
    assert args.args[2]["command"] == "restart"


async def test_owner_only_passes_owner_through(orchestrator):
    inner = AsyncMock()
    wrapped = orchestrator._owner_only(inner, "restart")
    update = _update(user_id=OWNER_ID)
    context = _context("owner")

    await wrapped(update, context)

    inner.assert_awaited_once()
    update.effective_message.reply_text.assert_not_awaited()


async def test_owner_only_uses_stamped_role_without_auth_manager(orchestrator):
    inner = AsyncMock()
    wrapped = orchestrator._owner_only(inner, "stop")
    update = _update()
    context = _context("guest", with_auth_manager=False)

    await wrapped(update, context)

    inner.assert_not_awaited()


async def test_owner_only_fails_closed_to_guest_when_role_unknown(orchestrator):
    """dv-09d8: an unresolvable role denies owner-only commands (guest)."""
    inner = AsyncMock()
    wrapped = orchestrator._owner_only(inner, "stop")
    update = _update(user_id=STRANGER_ID)
    context = _context("owner", with_auth_manager=False)
    context.user_data = {}

    await wrapped(update, context)

    inner.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once_with(
        "This command is owner-only."
    )


async def test_owner_only_env_owner_survives_unknown_role(tmp_dir):
    """Single-owner installs (id in ALLOWED_USERS) keep working even when
    the session and user_data stamp are both lost — the explicit carve-out."""
    settings = create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=True,
        allowed_users=str(OWNER_ID),
    )
    orch = MessageOrchestrator(settings, {})
    inner = AsyncMock()
    wrapped = orch._owner_only(inner, "stop")
    update = _update(user_id=OWNER_ID)
    context = _context("owner", with_auth_manager=False)
    context.user_data = {}

    await wrapped(update, context)

    inner.assert_awaited_once()


def test_owner_only_preserves_handler_name(orchestrator):
    """_inject_deps keys bypasses off handler.__name__ (e.g. sync_threads)."""

    async def sync_threads(update, context):
        pass

    assert orchestrator._owner_only(sync_threads, "sync_threads").__name__ == (
        "sync_threads"
    )


async def test_guest_menu_omits_owner_only_commands(orchestrator):
    owner_menu = {c.command for c in await orchestrator.get_bot_commands()}
    guest_menu = {c.command for c in await orchestrator.get_bot_commands(role="guest")}
    assert OWNER_ONLY_COMMANDS & owner_menu
    assert not OWNER_ONLY_COMMANDS & guest_menu
    # /status is registered for guests but unlisted since dv-a1c8 — /help
    # is what surfaces it.
    assert {"start", "new", "help", "usage"} <= guest_menu


# ---------------------------------------------------------------------------
# §3 Profile ACL
# ---------------------------------------------------------------------------


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
    )


def _acl_registry():
    return ProfileRegistry(
        [
            _profile("devops", "/tmp/dev", default=True, sandbox=False),
            _profile("general", "/tmp/gen", guest_ok=True),
            _profile("chef", "/tmp/chef", guest_ok=True),
        ]
    )


def test_allows_role_owner_always_passes():
    p = _profile("devops", "/tmp/dev")
    assert p.allows_role("owner")
    assert not p.allows_role("guest")


def test_apply_route_owner_unchanged():
    user_data = {"active_profile": "devops"}
    decision = apply_route("hello", user_data, _acl_registry(), role="owner")
    assert decision.working_dir == Path("/tmp/dev")


def test_apply_route_guest_default_is_general_not_registry_default():
    user_data: dict = {}
    decision = apply_route("hello", user_data, _acl_registry(), role="guest")
    assert decision.working_dir == Path("/tmp/gen")
    assert user_data["active_profile"] == "general"


def test_apply_route_guest_stored_disallowed_profile_falls_back():
    user_data = {"active_profile": "devops"}
    decision = apply_route("hello", user_data, _acl_registry(), role="guest")
    assert decision.working_dir == Path("/tmp/gen")
    assert user_data["active_profile"] == "general"


def test_apply_route_guest_allowed_profile_sticks():
    user_data = {"active_profile": "chef"}
    decision = apply_route("hello", user_data, _acl_registry(), role="guest")
    assert decision.working_dir == Path("/tmp/chef")
    assert user_data["active_profile"] == "chef"


def test_apply_route_guest_sticky_switch_denied():
    user_data = {"active_profile": "general"}
    with pytest.raises(ProfileAccessDenied) as exc:
        apply_route("/devops rm -rf /", user_data, _acl_registry(), role="guest")
    assert exc.value.slug == "devops"
    assert user_data["active_profile"] == "general"


def test_apply_route_guest_one_shot_denied():
    user_data = {"active_profile": "general"}
    with pytest.raises(ProfileAccessDenied):
        apply_route("@devops hi", user_data, _acl_registry(), role="guest")


def test_apply_route_guest_one_shot_allowed_profile_ok():
    user_data = {"active_profile": "general"}
    decision = apply_route("@chef dinner?", user_data, _acl_registry(), role="guest")
    assert decision.working_dir == Path("/tmp/chef")
    assert user_data["active_profile"] == "general"


def test_apply_route_guest_no_allowed_profiles_raises():
    registry = ProfileRegistry([_profile("devops", "/tmp/dev", default=True)])
    with pytest.raises(ProfileAccessDenied):
        apply_route("hello", {}, registry, role="guest")


def test_get_default_for_role_prefers_general():
    reg = _acl_registry()
    assert reg.get_default_for_role("owner").slug == "devops"
    assert reg.get_default_for_role("guest").slug == "general"


def test_get_default_for_role_falls_back_to_first_guest_allowed():
    reg = ProfileRegistry(
        [
            _profile("devops", "/tmp/dev", default=True),
            _profile("chef", "/tmp/chef", guest_ok=True),
        ]
    )
    assert reg.get_default_for_role("guest").slug == "chef"


def test_get_default_for_role_none_when_no_guest_profiles():
    reg = ProfileRegistry([_profile("devops", "/tmp/dev", default=True)])
    assert reg.get_default_for_role("guest") is None


async def test_switch_handler_denies_guest(orchestrator):
    handler = orchestrator._make_switch_handler("devops")
    update = _update()
    context = _context("guest")
    context.bot_data["profile_registry"] = _acl_registry()
    context.user_data["active_profile"] = "general"

    await handler(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "owner-only" in update.message.reply_text.await_args.args[0]
    assert context.user_data["active_profile"] == "general"
    context.bot_data["storage"].log_bot_event.assert_awaited_once()
    assert (
        context.bot_data["storage"].log_bot_event.await_args.args[1]
        == "profile_access_denied"
    )


async def test_switch_handler_allows_guest_into_permitted_profile(orchestrator):
    orchestrator._save_user_prefs = AsyncMock()
    handler = orchestrator._make_switch_handler("chef")
    update = _update()
    update.message.text = "/chef"
    context = _context("guest")
    context.bot_data["profile_registry"] = _acl_registry()

    await handler(update, context)

    assert context.user_data["active_profile"] == "chef"
    reply = update.message.reply_text.await_args.args[0]
    assert "switched" in reply


async def test_agents_picker_callback_denies_guest(orchestrator):
    context = _context("guest")
    context.bot_data["profile_registry"] = _acl_registry()
    query = MagicMock()
    query.from_user.id = GUEST_ID
    query.edit_message_text = AsyncMock()

    await orchestrator._handle_agent_callback(query, context, "agent:devops")

    assert "owner-only" in query.edit_message_text.await_args.args[0]
    assert "active_profile" not in context.user_data


# ---------------------------------------------------------------------------
# §3 Hard invariant: sandbox=false can never be guest-allowed
# ---------------------------------------------------------------------------


def _write_profiles(tmp_path, body):
    p = tmp_path / "profiles.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_GUEST_FULL_ACCESS = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: /tmp/dev
    memory_dir: /tmp/dev/m
    persona: /tmp/dev/p.md
    default: true
    sandbox: false
    permission_policy:
      allowed_roles: [owner, guest]
"""


def test_load_rejects_guest_on_explicit_sandbox_false(tmp_path):
    cfg = _write_profiles(tmp_path, _GUEST_FULL_ACCESS)
    with pytest.raises(ValueError, match="never be guest-allowed"):
        load_profile_registry(cfg, sandbox_default=True)


def test_load_rejects_guest_when_global_default_unsandboxed(tmp_path):
    cfg = _write_profiles(
        tmp_path,
        """
profiles:
  - slug: general
    name: General
    working_dir: /tmp/gen
    memory_dir: /tmp/gen/m
    persona: /tmp/gen/p.md
    default: true
    permission_policy:
      allowed_roles: [owner, guest]
""",
    )
    # sandbox unset resolves via the global default (false here) -> refuse.
    with pytest.raises(ValueError, match="never be guest-allowed"):
        load_profile_registry(cfg, sandbox_default=False)
    # Same YAML is fine when the global default sandboxes it.
    reg = load_profile_registry(cfg, sandbox_default=True)
    assert reg.get_by_slug("general").allows_role("guest")


def test_load_allows_guest_on_explicitly_sandboxed_profile(tmp_path):
    cfg = _write_profiles(
        tmp_path,
        """
profiles:
  - slug: general
    name: General
    working_dir: /tmp/gen
    memory_dir: /tmp/gen/m
    persona: /tmp/gen/p.md
    default: true
    sandbox: true
    permission_policy:
      allowed_roles: [owner, guest]
""",
    )
    reg = load_profile_registry(cfg, sandbox_default=False)
    assert reg.get_by_slug("general").allows_role("guest")


def test_load_rejects_unknown_role(tmp_path):
    cfg = _write_profiles(
        tmp_path,
        """
profiles:
  - slug: general
    name: General
    working_dir: /tmp/gen
    memory_dir: /tmp/gen/m
    persona: /tmp/gen/p.md
    default: true
    permission_policy:
      allowed_roles: [owner, admin]
""",
    )
    with pytest.raises(ValueError, match="unknown roles"):
        load_profile_registry(cfg)


def test_load_default_policy_is_owner_only(tmp_path):
    cfg = _write_profiles(
        tmp_path,
        """
profiles:
  - slug: general
    name: General
    working_dir: /tmp/gen
    memory_dir: /tmp/gen/m
    persona: /tmp/gen/p.md
    default: true
""",
    )
    profile = load_profile_registry(cfg).get_by_slug("general")
    assert profile.allows_role("owner")
    assert not profile.allows_role("guest")
