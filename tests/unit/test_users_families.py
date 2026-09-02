"""Tests for users & families v1 (stages A+B).

Covers docs/plans/2026-07-19-users-families-v1.md:
- Stage A: bot_users/families migration + repository CRUD, GUEST_USERS boot
  seeding idempotence, DB-union auth resolution with cache invalidation,
  /adduser + /rmuser owner commands.
- Stage B: /family add|delete|manage guardrails, cap, owner notification,
  source-sensitive revocation, manage formatting.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.formatting import format_family, parse_telegram_id
from src.bot.orchestrator import OWNER_ONLY_COMMANDS, MessageOrchestrator
from src.config import create_test_config
from src.events.types import AgentResponseEvent
from src.security.auth import (
    ROLE_GUEST,
    ROLE_OWNER,
    AuthenticationManager,
    WhitelistAuthProvider,
)
from src.storage.database import DatabaseManager
from src.storage.user_repository import (
    SOURCE_ADMIN,
    SOURCE_ENV,
    SOURCE_FAMILY,
    BotUserRepository,
    UserStore,
)

OWNER_ID = 111
GUEST_ID = 222
TARGET_ID = 333
OTHER_ID = 444


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "users.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def repo(db):
    return BotUserRepository(db)


@pytest.fixture
async def store(repo):
    s = UserStore(repo)
    await s.refresh()
    return s


# ---------------------------------------------------------------------------
# Stage A: repository CRUD + seeding
# ---------------------------------------------------------------------------


class TestBotUserRepository:
    async def test_add_and_get_user(self, repo):
        await repo.add_user(GUEST_ID, source=SOURCE_ADMIN, added_by=OWNER_ID)
        user = await repo.get_user(GUEST_ID)
        assert user.telegram_id == GUEST_ID
        assert user.role == "guest"
        assert user.source == SOURCE_ADMIN
        assert user.added_by == OWNER_ID
        assert user.family_id is None
        assert user.created_at is not None

    async def test_get_missing_user_is_none(self, repo):
        assert await repo.get_user(GUEST_ID) is None

    async def test_remove_user(self, repo):
        await repo.add_user(GUEST_ID)
        assert await repo.remove_user(GUEST_ID) is True
        assert await repo.get_user(GUEST_ID) is None
        assert await repo.remove_user(GUEST_ID) is False

    async def test_list_and_count_users(self, repo):
        await repo.add_user(GUEST_ID)
        await repo.add_user(TARGET_ID)
        users = await repo.list_users()
        assert [u.telegram_id for u in users] == [GUEST_ID, TARGET_ID]
        assert await repo.count_users() == 2

    async def test_seed_env_guests_idempotent(self, repo):
        assert await repo.seed_env_guests([GUEST_ID, TARGET_ID]) == 2
        assert await repo.seed_env_guests([GUEST_ID, TARGET_ID]) == 0
        user = await repo.get_user(GUEST_ID)
        assert user.role == "guest"
        assert user.source == SOURCE_ENV

    async def test_seed_never_downgrades_admin_row(self, repo):
        await repo.add_user(GUEST_ID, source=SOURCE_ADMIN, added_by=OWNER_ID)
        await repo.seed_env_guests([GUEST_ID])
        user = await repo.get_user(GUEST_ID)
        assert user.source == SOURCE_ADMIN
        assert user.added_by == OWNER_ID

    async def test_seed_empty_list_noop(self, repo):
        assert await repo.seed_env_guests([]) == 0

    async def test_seed_env_owners_creates_owner_rows(self, repo):
        """ALLOWED_USERS ids become real rows, not env-only access (dv-5c83)."""
        assert await repo.seed_env_owners([OWNER_ID]) == 1
        assert await repo.seed_env_owners([OWNER_ID]) == 0
        user = await repo.get_user(OWNER_ID)
        assert user.role == ROLE_OWNER
        assert user.source == SOURCE_ENV

    async def test_seed_env_owners_does_not_promote_an_existing_guest(self, repo):
        """Never re-source or re-role a row; the guest stays a guest."""
        await repo.seed_env_guests([GUEST_ID])
        assert await repo.seed_env_owners([GUEST_ID]) == 0
        assert (await repo.get_user(GUEST_ID)).role == ROLE_GUEST

    async def test_seed_env_owners_empty_list_noop(self, repo):
        assert await repo.seed_env_owners([]) == 0

    async def test_family_crud(self, repo):
        family = await repo.create_family(OWNER_ID)
        assert family.created_by == OWNER_ID
        assert (await repo.get_family(family.id)).id == family.id
        assert await repo.get_family(999) is None

        await repo.add_user(GUEST_ID, family_id=family.id, source=SOURCE_FAMILY)
        await repo.add_user(TARGET_ID)
        await repo.set_family(TARGET_ID, family.id)

        members = await repo.list_family_members(family.id)
        assert {m.telegram_id for m in members} == {GUEST_ID, TARGET_ID}
        assert await repo.count_family_members(family.id) == 2

        await repo.set_family(TARGET_ID, None)
        assert await repo.count_family_members(family.id) == 1

    async def test_remove_user_clears_family_membership(self, repo):
        family = await repo.create_family(OWNER_ID)
        await repo.add_user(GUEST_ID, family_id=family.id, source=SOURCE_FAMILY)
        await repo.remove_user(GUEST_ID)
        assert await repo.count_family_members(family.id) == 0


# ---------------------------------------------------------------------------
# Stage A: UserStore cache
# ---------------------------------------------------------------------------


class TestUserStoreCache:
    async def test_owner_ids_reflects_seeded_owners(self, store):
        """main.py gates the auth provider on this: an owner that lives only
        in the DB must still produce a whitelist, not the no-providers refusal
        (dv-5c83)."""
        assert store.owner_ids() == []
        await store.seed_env_owners([OWNER_ID])
        assert store.owner_ids() == [OWNER_ID]
        assert store.guest_ids() == []

    async def test_mutations_refresh_snapshot_inline(self, store):
        assert store.get_role_cached(GUEST_ID) is None
        await store.add_user(GUEST_ID)
        assert store.get_role_cached(GUEST_ID) == "guest"
        await store.remove_user(GUEST_ID)
        assert store.get_role_cached(GUEST_ID) is None

    async def test_invalidate_and_ensure_fresh(self, store, repo):
        # External write bypassing the store: snapshot is stale until
        # invalidated + refreshed.
        await repo.add_user(GUEST_ID)
        assert store.get_role_cached(GUEST_ID) is None
        await store.ensure_fresh()  # not dirty -> still stale
        assert store.get_role_cached(GUEST_ID) is None
        store.invalidate()
        await store.ensure_fresh()
        assert store.get_role_cached(GUEST_ID) == "guest"

    async def test_seed_refreshes_snapshot(self, store):
        await store.seed_env_guests([GUEST_ID])
        assert store.get_role_cached(GUEST_ID) == "guest"

    async def test_guest_ids(self, store):
        await store.add_user(GUEST_ID)
        await store.add_user(TARGET_ID, role="owner")
        assert store.guest_ids() == [GUEST_ID]


# ---------------------------------------------------------------------------
# Stage A: auth resolution (env + DB union, precedence, invalidation)
# ---------------------------------------------------------------------------


class TestAuthResolution:
    def _provider(self, store, allowed=None, guests=None):
        return WhitelistAuthProvider(
            allowed or [], guest_users=guests, user_store=store
        )

    async def test_db_guest_resolves_guest(self, store):
        await store.add_user(GUEST_ID)
        assert self._provider(store).resolve_role(GUEST_ID) == ROLE_GUEST

    async def test_db_owner_resolves_owner(self, store):
        await store.add_user(GUEST_ID, role="owner")
        assert self._provider(store).resolve_role(GUEST_ID) == ROLE_OWNER

    async def test_env_owner_beats_db_guest(self, store):
        await store.add_user(OWNER_ID)  # db guest row
        provider = self._provider(store, allowed=[OWNER_ID])
        assert provider.resolve_role(OWNER_ID) == ROLE_OWNER

    async def test_env_guest_still_honored_without_db_row(self, store):
        provider = self._provider(store, guests=[GUEST_ID])
        assert provider.resolve_role(GUEST_ID) == ROLE_GUEST

    async def test_unknown_user_denied(self, store):
        assert self._provider(store).resolve_role(OTHER_ID) is None

    async def test_authenticate_refreshes_dirty_cache(self, store, repo):
        provider = self._provider(store)
        assert await provider.authenticate(GUEST_ID, {}) is False
        await repo.add_user(GUEST_ID)  # external write
        store.invalidate()
        assert await provider.authenticate(GUEST_ID, {}) is True

    async def test_manager_resolves_db_guest_role(self, store):
        await store.add_user(GUEST_ID)
        manager = AuthenticationManager([self._provider(store)])
        assert await manager.authenticate_user(GUEST_ID)
        assert manager.get_user_role(GUEST_ID) == ROLE_GUEST

    async def test_removal_then_new_auth_denied(self, store):
        await store.add_user(GUEST_ID)
        manager = AuthenticationManager([self._provider(store)])
        assert await manager.authenticate_user(GUEST_ID)
        await store.remove_user(GUEST_ID)
        manager.end_session(GUEST_ID)
        assert not await manager.authenticate_user(GUEST_ID)
        assert manager.get_user_role(GUEST_ID) is None

    def test_provider_without_store_unchanged(self):
        provider = WhitelistAuthProvider([OWNER_ID], guest_users=[GUEST_ID])
        assert provider.resolve_role(OWNER_ID) == ROLE_OWNER
        assert provider.resolve_role(GUEST_ID) == ROLE_GUEST
        assert provider.resolve_role(OTHER_ID) is None


# ---------------------------------------------------------------------------
# Command plumbing helpers
# ---------------------------------------------------------------------------


def _orchestrator(tmp_path, **overrides):
    settings = create_test_config(
        approved_directory=str(tmp_path), agentic_mode=True, **overrides
    )
    deps = {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }
    return MessageOrchestrator(settings, deps)


def _context(role, store):
    context = MagicMock()
    context.user_data = {"role": role}
    auth_manager = MagicMock()
    auth_manager.get_user_role = MagicMock(return_value=role)
    auth_manager.end_session = MagicMock()
    storage = MagicMock()
    storage.log_bot_event = AsyncMock()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    context.bot_data = {
        "auth_manager": auth_manager,
        "storage": storage,
        "user_store": store,
        "event_bus": event_bus,
    }
    return context


def _update(user_id, args=""):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    update.message.reply_text = AsyncMock()
    return update


def _reply(update):
    return update.effective_message.reply_text.await_args.args[0]


def test_parse_telegram_id():
    assert parse_telegram_id(["123456"]) == 123456
    assert parse_telegram_id([]) is None
    assert parse_telegram_id(["abc"]) is None
    assert parse_telegram_id(["-5"]) is None
    assert parse_telegram_id(["0"]) is None
    assert parse_telegram_id([str(10**16)]) is None
    assert parse_telegram_id(["1", "2"]) is None


def test_admin_commands_are_owner_only_and_family_is_not():
    assert "adduser" in OWNER_ONLY_COMMANDS
    assert "rmuser" in OWNER_ONLY_COMMANDS
    assert "family" not in OWNER_ONLY_COMMANDS


async def test_guest_menu_hides_admin_commands_keeps_family(tmp_path):
    orch = _orchestrator(tmp_path)
    owner_menu = {c.command for c in await orch.get_bot_commands()}
    guest_menu = {c.command for c in await orch.get_bot_commands(role="guest")}
    # Admin commands are unlisted for everyone since dv-a1c8 (registered and
    # working, surfaced by /help); the point here is that the guest MENU never
    # advertises them while /family stays visible to the role that needs it.
    assert not {"adduser", "rmuser"} & owner_menu
    assert "adduser" not in guest_menu
    assert "rmuser" not in guest_menu
    assert "family" in guest_menu


async def test_owner_only_wrapper_blocks_guest_adduser(tmp_path, store):
    orch = _orchestrator(tmp_path)
    wrapped = orch._owner_only(orch.agentic_adduser, "adduser")
    update = _update(GUEST_ID)
    context = _context("guest", store)
    context.args = [str(TARGET_ID)]

    await wrapped(update, context)

    assert _reply(update) == "This command is owner-only."
    assert await store.get_user(TARGET_ID) is None


# ---------------------------------------------------------------------------
# Stage A: /adduser and /rmuser
# ---------------------------------------------------------------------------


class TestAddUser:
    async def test_happy_path(self, tmp_path, store):
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_adduser(update, context)

        user = await store.get_user(TARGET_ID)
        assert user.role == "guest"
        assert user.source == SOURCE_ADMIN
        assert user.added_by == OWNER_ID
        assert store.get_role_cached(TARGET_ID) == "guest"
        assert _reply(update) == f"Added guest {TARGET_ID}."
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "command_adduser"

    async def test_invalid_id_rejected(self, tmp_path, store):
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["not-a-number"]

        await orch.agentic_adduser(update, context)

        assert "Usage" in _reply(update)

    async def test_existing_user_not_overwritten(self, tmp_path, store):
        await store.add_user(TARGET_ID, source=SOURCE_ENV)
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_adduser(update, context)

        assert "already has access" in _reply(update)
        assert (await store.get_user(TARGET_ID)).source == SOURCE_ENV

    async def test_env_owner_refused(self, tmp_path, store):
        orch = _orchestrator(tmp_path, allowed_users=str(OWNER_ID))
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(OWNER_ID)]

        await orch.agentic_adduser(update, context)

        assert "already an owner" in _reply(update)

    async def test_missing_store_friendly_error(self, tmp_path):
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", None)
        context.bot_data["user_store"] = None
        context.args = [str(TARGET_ID)]

        await orch.agentic_adduser(update, context)

        assert "not available" in _reply(update)


class TestRmUser:
    async def test_happy_path_ends_session(self, tmp_path, store):
        await store.add_user(TARGET_ID)
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_rmuser(update, context)

        assert await store.get_user(TARGET_ID) is None
        assert store.get_role_cached(TARGET_ID) is None
        context.bot_data["auth_manager"].end_session.assert_called_once_with(TARGET_ID)
        assert _reply(update) == f"Removed {TARGET_ID}."
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "command_rmuser"

    async def test_clears_family_membership(self, tmp_path, store):
        family = await store.create_family(OWNER_ID)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=family.id)
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_rmuser(update, context)

        assert await store.count_family_members(family.id) == 0

    async def test_unknown_user(self, tmp_path, store):
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_rmuser(update, context)

        assert "not a managed user" in _reply(update)

    async def test_env_owner_refused(self, tmp_path, store):
        orch = _orchestrator(tmp_path, allowed_users=str(OWNER_ID))
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(OWNER_ID)]

        await orch.agentic_rmuser(update, context)

        assert "ALLOWED_USERS" in _reply(update)

    async def test_env_guest_gets_warning(self, tmp_path, store):
        await store.seed_env_guests([TARGET_ID])
        orch = _orchestrator(tmp_path, guest_users=str(TARGET_ID))
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = [str(TARGET_ID)]

        await orch.agentic_rmuser(update, context)

        assert "GUEST_USERS" in _reply(update)


# ---------------------------------------------------------------------------
# Stage B: /family add
# ---------------------------------------------------------------------------


def test_family_max_members_defaults_to_3():
    assert create_test_config().family_max_members == 3
    assert create_test_config(family_max_members="5").family_max_members == 5


class TestFamilyAdd:
    async def test_auto_creates_family_for_db_owner_caller(self, tmp_path, store):
        await store.add_user(OWNER_ID, role="owner")  # db owner, no family
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        caller = await store.get_user(OWNER_ID)
        target = await store.get_user(TARGET_ID)
        assert caller.family_id is not None
        assert target.family_id == caller.family_id
        assert target.role == "guest"
        assert target.source == SOURCE_FAMILY
        assert target.added_by == OWNER_ID
        assert store.get_role_cached(TARGET_ID) == "guest"
        assert "2/3 members" in _reply(update)

    async def test_guest_caller_denied(self, tmp_path, store):
        """/family add is owner-only (dv-09d8): guests cannot grow the family."""
        await store.add_user(GUEST_ID)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert await store.get_user(TARGET_ID) is None
        assert "Only owners can add family members" in _reply(update)
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_add_denied"
        assert log.await_args.args[2]["reason"] == "not_owner"
        context.bot_data["event_bus"].publish.assert_not_awaited()

    async def test_env_owner_caller_gets_membership_row(self, tmp_path, store):
        orch = _orchestrator(tmp_path, allowed_users=str(OWNER_ID))
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        caller = await store.get_user(OWNER_ID)
        assert caller is not None
        assert caller.role == "owner"
        assert caller.family_id is not None
        family = await store.get_family(caller.family_id)
        assert family.created_by == OWNER_ID

    async def test_cap_refusal(self, tmp_path, store):
        family = await store.create_family(OWNER_ID)
        await store.add_user(OWNER_ID, role="owner", family_id=family.id)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=family.id)
        await store.add_user(OTHER_ID, source=SOURCE_FAMILY, family_id=family.id)
        orch = _orchestrator(tmp_path)  # cap = 3, family already full
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", "555"]

        await orch.agentic_family(update, context)

        assert "full (3/3" in _reply(update)
        assert await store.get_user(555) is None
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_add_denied"

    async def test_target_in_other_family_refused(self, tmp_path, store):
        other_family = await store.create_family(OTHER_ID)
        await store.add_user(TARGET_ID, family_id=other_family.id)
        await store.add_user(OWNER_ID, role="owner")
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert "another family" in _reply(update)

    async def test_target_already_in_own_family(self, tmp_path, store):
        family = await store.create_family(OWNER_ID)
        await store.add_user(OWNER_ID, role="owner", family_id=family.id)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=family.id)
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert "already in your family" in _reply(update)

    async def test_owner_target_joins_membership_only(self, tmp_path, store):
        """Owners may join a family for fact-sharing; role/source untouched."""
        await store.add_user(OTHER_ID, role="owner")  # db-owner caller
        orch = _orchestrator(tmp_path, allowed_users=str(OWNER_ID))
        update = _update(OTHER_ID)
        context = _context("owner", store)
        context.args = ["add", str(OWNER_ID)]

        await orch.agentic_family(update, context)

        target = await store.get_user(OWNER_ID)
        caller = await store.get_user(OTHER_ID)
        assert target is not None
        assert target.role == "owner"
        assert target.source == SOURCE_ENV
        assert target.family_id == caller.family_id
        assert "only shares family facts" in _reply(update)

    async def test_family_delete_never_revokes_owner(self, tmp_path, store):
        """Even a family-sourced row with owner role is detached, not revoked."""
        family = await store.create_family(GUEST_ID)
        await store.add_user(GUEST_ID, family_id=family.id)
        await store.add_user(
            OWNER_ID, role="owner", source=SOURCE_FAMILY, family_id=family.id
        )
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["delete", str(OWNER_ID)]

        await orch.agentic_family(update, context)

        target = await store.get_user(OWNER_ID)
        assert target is not None
        assert target.family_id is None
        assert "keep bot access" in _reply(update)

    async def test_owner_notified_on_add(self, tmp_path, store):
        await store.add_user(OWNER_ID, role="owner")
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        publish = context.bot_data["event_bus"].publish
        publish.assert_awaited_once()
        event = publish.await_args.args[0]
        assert isinstance(event, AgentResponseEvent)
        assert event.chat_id == 0  # broadcast to owner notification chats
        assert str(OWNER_ID) in event.text
        assert str(TARGET_ID) in event.text
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_add"

    async def test_self_add_refused(self, tmp_path, store):
        await store.add_user(OWNER_ID, role="owner")
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["add", str(OWNER_ID)]

        await orch.agentic_family(update, context)

        assert "yourself" in _reply(update)


# ---------------------------------------------------------------------------
# Stage B: /family delete
# ---------------------------------------------------------------------------


class TestFamilyDelete:
    async def _family_with(self, store, caller_source=SOURCE_ADMIN):
        family = await store.create_family(GUEST_ID)
        await store.add_user(GUEST_ID, source=caller_source, family_id=family.id)
        return family

    async def test_family_sourced_member_fully_revoked(self, tmp_path, store):
        family = await self._family_with(store)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=family.id)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["delete", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert await store.get_user(TARGET_ID) is None
        assert store.get_role_cached(TARGET_ID) is None
        context.bot_data["auth_manager"].end_session.assert_called_once_with(TARGET_ID)
        assert "revoked" in _reply(update)
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_delete"
        assert log.await_args.args[2]["revoked"] is True

    async def test_env_sourced_member_keeps_access(self, tmp_path, store):
        family = await self._family_with(store)
        await store.add_user(TARGET_ID, source=SOURCE_ENV, family_id=family.id)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["delete", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        target = await store.get_user(TARGET_ID)
        assert target is not None
        assert target.family_id is None
        context.bot_data["auth_manager"].end_session.assert_not_called()
        assert "keep bot access" in _reply(update)

    async def test_guest_cannot_delete_from_other_family(self, tmp_path, store):
        other_family = await store.create_family(OTHER_ID)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=other_family.id)
        await store.add_user(GUEST_ID)  # no family
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["delete", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert await store.get_user(TARGET_ID) is not None
        assert "own family" in _reply(update)
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_delete_denied"

    async def test_owner_can_delete_from_any_family(self, tmp_path, store):
        other_family = await store.create_family(OTHER_ID)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=other_family.id)
        orch = _orchestrator(tmp_path)
        update = _update(OWNER_ID)
        context = _context("owner", store)
        context.args = ["delete", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert await store.get_user(TARGET_ID) is None
        assert "revoked" in _reply(update)

    async def test_target_not_in_family(self, tmp_path, store):
        await store.add_user(TARGET_ID)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["delete", str(TARGET_ID)]

        await orch.agentic_family(update, context)

        assert "not in a family" in _reply(update)


# ---------------------------------------------------------------------------
# Stage B: /family manage + formatting
# ---------------------------------------------------------------------------


class TestFamilyManage:
    async def test_no_family_yet(self, tmp_path, store):
        await store.add_user(GUEST_ID)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = []

        await orch.agentic_family(update, context)

        assert "don't have a family yet" in _reply(update)

    async def test_manage_lists_members_html(self, tmp_path, store):
        family = await store.create_family(GUEST_ID)
        await store.add_user(GUEST_ID, family_id=family.id)
        await store.add_user(TARGET_ID, source=SOURCE_FAMILY, family_id=family.id)
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["manage"]

        await orch.agentic_family(update, context)

        call = update.effective_message.reply_text.await_args
        text = call.args[0]
        assert call.kwargs.get("parse_mode") == "HTML"
        assert "2/3 members" in text
        assert f"<code>{GUEST_ID}</code>" in text
        assert f"<code>{TARGET_ID}</code>" in text
        log = context.bot_data["storage"].log_bot_event
        assert log.await_args.args[1] == "family_manage"

    async def test_unknown_subcommand_usage(self, tmp_path, store):
        orch = _orchestrator(tmp_path)
        update = _update(GUEST_ID)
        context = _context("guest", store)
        context.args = ["frobnicate"]

        await orch.agentic_family(update, context)

        assert "Usage" in _reply(update)

    async def test_format_family_output(self, store):
        family = await store.create_family(GUEST_ID)
        await store.add_user(GUEST_ID, family_id=family.id)
        members = await store.list_family_members(family.id)
        family = await store.get_family(family.id)

        text = format_family(family, members, cap=3)

        assert "1/3 members" in text
        assert "Created:" in text
        assert "guest" in text
