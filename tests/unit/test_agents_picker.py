from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.orchestration.routing import seed_prefs_into_user_data
from src.bot.orchestrator import MessageOrchestrator


def test_seed_populates_missing_keys():
    ud = {}
    seed_prefs_into_user_data(
        ud,
        {
            "active_profile": "channel",
            "agent_backend": "claude",
            "agent_model": "claude",
        },
    )
    assert ud["active_profile"] == "channel"
    assert ud["agent_backend"] == "claude"
    assert ud["agent_model"] == "claude"


def test_seed_does_not_overwrite_existing():
    ud = {"active_profile": "general"}
    seed_prefs_into_user_data(ud, {"active_profile": "channel"})
    assert ud["active_profile"] == "general"


def test_seed_ignores_none_values():
    ud = {}
    seed_prefs_into_user_data(
        ud,
        {"active_profile": "channel", "agent_backend": None, "agent_model": None},
    )
    assert ud["active_profile"] == "channel"
    assert "agent_backend" not in ud


def test_seed_handles_none_prefs():
    ud = {}
    seed_prefs_into_user_data(ud, None)
    assert ud == {}


class _FakeProfile:
    def __init__(self, slug, name, working_dir, personal_memory=False):
        self.slug = slug
        self.name = name
        self.working_dir = working_dir
        # Decides whether an owner gets their own workspace under this profile
        # (see registry.needs_own_workspace); these tests use the shared root.
        self.personal_memory = personal_memory

    def allows_role(self, role):
        # Mirrors ProfileDefinition.allows_role for these owner-path tests.
        return role == "owner"


class _FakeRegistry:
    def __init__(self, profiles):
        self.profiles = profiles
        self._by = {p.slug: p for p in profiles}

    def get_default(self):
        return self.profiles[0]

    def get_by_slug(self, slug):
        return self._by.get(slug)

    def get_enabled_by_slug(self, slug):
        return self._by.get(slug)

    @property
    def enabled_profiles(self):
        return list(self.profiles)


def _orchestrator():
    settings = SimpleNamespace(
        agentic_mode=True, agent_backend="claude", agent_model="claude"
    )
    return MessageOrchestrator(settings, {})


def _ctx(storage):
    # Role stamped explicitly: since dv-09d8 an unresolvable role fails
    # closed to guest, and guests are denied the devops/channel profiles.
    return SimpleNamespace(
        user_data={"role": "owner"},
        bot_data={
            "storage": storage,
            "profile_registry": _FakeRegistry(
                [
                    _FakeProfile("devops", "DevOps", "/a"),
                    _FakeProfile("channel", "Channel", "/b"),
                ]
            ),
        },
    )


async def test_agent_callback_switches_and_persists():
    orch = _orchestrator()
    storage = SimpleNamespace(preferences=AsyncMock())
    context = _ctx(storage)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        edit_message_text=AsyncMock(),
    )

    await orch._handle_agent_callback(query, context, "agent:channel")

    assert context.user_data["active_profile"] == "channel"
    assert context.user_data["current_directory"] == "/b"
    storage.preferences.set.assert_awaited_once_with(42, active_profile="channel")
    query.edit_message_text.assert_awaited_once()


async def test_agent_callback_rejects_unknown_slug():
    orch = _orchestrator()
    storage = SimpleNamespace(preferences=AsyncMock())
    context = _ctx(storage)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        edit_message_text=AsyncMock(),
    )

    await orch._handle_agent_callback(query, context, "agent:nope")

    assert "active_profile" not in context.user_data
    storage.preferences.set.assert_not_awaited()
    args, _ = query.edit_message_text.call_args
    assert "nope" in args[0]


# ---------------------------------------------------------------------------
# _pin_switch_message: pin the switch confirmation, unpinning only the
# previous one this method pinned (any manual pins are left alone).
# ---------------------------------------------------------------------------


def _fake_message(chat_id, message_id):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id)


class _FakePreferences:
    """Dict-backed stand-in -- a loose Mock would auto-satisfy truthiness
    checks and mask real "no previous pin" / "same id" branches."""

    def __init__(self, initial=None):
        self._store = dict(initial or {})
        self.set_calls = []

    async def get(self, user_id):
        return dict(self._store) if self._store else None

    async def set(self, user_id, **fields):
        self.set_calls.append((user_id, fields))
        self._store.update(fields)


def _ctx_with_bot(storage, bot):
    return SimpleNamespace(user_data={}, bot_data={"storage": storage}, bot=bot)


def _fake_bot(**overrides):
    return SimpleNamespace(
        unpin_chat_message=overrides.get("unpin_chat_message", AsyncMock()),
        pin_chat_message=overrides.get("pin_chat_message", AsyncMock()),
    )


async def test_pin_switch_message_unpins_previous_and_pins_new():
    orch = _orchestrator()
    prefs = _FakePreferences({"pinned_switch_message_id": 111})
    bot = _fake_bot()
    context = _ctx_with_bot(SimpleNamespace(preferences=prefs), bot)

    await orch._pin_switch_message(context, 42, _fake_message(555, 222))

    bot.unpin_chat_message.assert_awaited_once_with(chat_id=555, message_id=111)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=555, message_id=222, disable_notification=True
    )
    assert prefs.set_calls == [(42, {"pinned_switch_message_id": 222})]


async def test_pin_switch_message_no_previous_pin_skips_unpin():
    orch = _orchestrator()
    prefs = _FakePreferences(None)
    bot = _fake_bot()
    context = _ctx_with_bot(SimpleNamespace(preferences=prefs), bot)

    await orch._pin_switch_message(context, 42, _fake_message(555, 222))

    bot.unpin_chat_message.assert_not_awaited()
    bot.pin_chat_message.assert_awaited_once()


async def test_pin_switch_message_same_id_skips_unpin():
    orch = _orchestrator()
    prefs = _FakePreferences({"pinned_switch_message_id": 222})
    bot = _fake_bot()
    context = _ctx_with_bot(SimpleNamespace(preferences=prefs), bot)

    await orch._pin_switch_message(context, 42, _fake_message(555, 222))

    bot.unpin_chat_message.assert_not_awaited()
    bot.pin_chat_message.assert_awaited_once()


async def test_pin_switch_message_pin_failure_is_best_effort():
    orch = _orchestrator()
    prefs = _FakePreferences(None)
    bot = _fake_bot(pin_chat_message=AsyncMock(side_effect=RuntimeError("no rights")))
    context = _ctx_with_bot(SimpleNamespace(preferences=prefs), bot)

    await orch._pin_switch_message(context, 42, _fake_message(555, 222))  # no raise

    assert prefs.set_calls == []  # never persisted a pin that failed


async def test_pin_switch_message_ignores_stale_unpin_failure():
    """Old pinned message already gone (user unpinned by hand, etc.) --
    unpin errors are swallowed and the new pin still goes through."""
    orch = _orchestrator()
    prefs = _FakePreferences({"pinned_switch_message_id": 111})
    bot = _fake_bot(
        unpin_chat_message=AsyncMock(side_effect=RuntimeError("message not found"))
    )
    context = _ctx_with_bot(SimpleNamespace(preferences=prefs), bot)

    await orch._pin_switch_message(context, 42, _fake_message(555, 222))

    bot.pin_chat_message.assert_awaited_once()
    assert prefs.set_calls == [(42, {"pinned_switch_message_id": 222})]
