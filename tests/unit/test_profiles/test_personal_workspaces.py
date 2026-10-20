"""`personal_memory` must isolate FILES, not just memory facts.

The declaration on `coach` is "health facts stay strictly per-user, never
shared". The memory layer honored it; the filesystem did not — per-user dirs
were created only for role != owner, so every owner-role user shared one
directory holding one person's nutrition/training/recovery logs, health CSVs
and photos. Verified live 2026-08-02.
"""

from pathlib import Path

import pytest

from src.bot.orchestration.routing import apply_route
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    agent_id_for_working_dir,
    ensure_workspace_for,
    needs_own_workspace,
    user_media_root,
    workspace_for,
)


def _profile(tmp_path, slug="coach", **kwargs):
    return ProfileDefinition(
        slug=slug,
        name=slug.title(),
        working_dir=tmp_path / slug,
        memory_dir=tmp_path / slug / "memory",
        persona=tmp_path / slug / "persona.md",
        default=True,
        **kwargs,
    )


class TestUserMediaRoot:
    """dv-9ebe: uploads split per user even where the workspace does not.

    A shared profile's root is readable by every guest allowed to select it,
    so an upload written there leaks. ``users/``/``guests/`` is the tree
    ReadScope denies guests, so uploads must land inside it regardless of the
    `shared:` flag.
    """

    def test_owner_on_a_shared_profile_still_gets_a_private_upload_dir(self, tmp_path):
        """The leak: workspace_for returns the guest-readable root here."""
        profile = _profile(tmp_path, personal_memory=False)  # `shared: true`
        assert workspace_for(profile, "owner", 111) == profile.working_dir
        assert user_media_root(profile, "owner", 111) == (
            profile.working_dir / "users" / "111"
        )

    def test_owner_on_a_personal_profile_matches_their_workspace(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=True)
        assert user_media_root(profile, "owner", 111) == workspace_for(
            profile, "owner", 111
        )

    def test_guest_gets_their_own_guest_dir(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=False)
        assert user_media_root(profile, "guest", 222) == (
            profile.working_dir / "guests" / "222"
        )

    def test_two_users_never_collide(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=False)
        assert user_media_root(profile, "owner", 111) != user_media_root(
            profile, "owner", 222
        )

    def test_stays_under_the_profile_root(self, tmp_path):
        """allowed_image_roots() is the turn's working dir; an upload outside
        it would stop being deliverable back to the user."""
        profile = _profile(tmp_path, personal_memory=False)
        for role, uid in (("owner", 111), ("guest", 222)):
            assert user_media_root(profile, role, uid).is_relative_to(
                profile.working_dir
            )


class TestNeedsOwnWorkspace:
    def test_guest_always_gets_one(self, tmp_path):
        assert needs_own_workspace(_profile(tmp_path), "guest") is True

    def test_owner_on_a_shared_profile_uses_the_root(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=False)  # `shared: true`
        assert needs_own_workspace(profile, "owner") is False

    def test_owner_on_a_personal_profile_gets_one(self, tmp_path):
        """The regression: this returned False, so two spouses shared one dir."""
        profile = _profile(tmp_path, personal_memory=True)
        assert needs_own_workspace(profile, "owner") is True


class TestWorkspacePaths:
    def test_owner_personal_profile_gets_users_dir(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=True)
        assert workspace_for(profile, "owner", 111111111) == (
            tmp_path / "coach" / "users" / "111111111"
        )

    def test_two_owners_do_not_collide(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=True)
        a = workspace_for(profile, "owner", 111111111)
        b = workspace_for(profile, "owner", 222222222)
        assert a != b

    def test_guest_keeps_the_guests_dir(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=True)
        assert workspace_for(profile, "guest", 7) == (
            tmp_path / "coach" / "guests" / "7"
        )

    def test_shared_profile_owner_keeps_the_root(self, tmp_path):
        profile = _profile(tmp_path, slug="devops", personal_memory=False)
        assert workspace_for(profile, "owner", 111111111) == profile.working_dir

    def test_no_user_id_falls_back_to_the_root(self, tmp_path):
        """Callers without a resolved user must not land in users/None."""
        profile = _profile(tmp_path, personal_memory=True)
        assert workspace_for(profile, "owner", None) == profile.working_dir

    def test_ensure_creates_only_the_per_user_dir(self, tmp_path):
        profile = _profile(tmp_path, personal_memory=True)
        path = ensure_workspace_for(profile, "owner", 42)
        assert path.is_dir()
        assert path == tmp_path / "coach" / "users" / "42"


class TestMemoryScopeIsUnchanged:
    """The new dir must resolve to the SAME agent_id the personal_memory
    branch already produced, or the switch orphans every existing fact."""

    @pytest.fixture
    def registry(self, tmp_path):
        return ProfileRegistry([_profile(tmp_path, personal_memory=True)])

    def test_users_dir_maps_to_the_same_scope_as_before(self, registry, tmp_path):
        root = tmp_path / "coach"
        before = agent_id_for_working_dir(registry, root, user_id=111111111)
        after = agent_id_for_working_dir(
            registry, root / "users" / "111111111", user_id=111111111
        )
        assert before == after == "coach#u111111111"

    def test_guests_dir_still_resolves(self, registry, tmp_path):
        assert (
            agent_id_for_working_dir(registry, tmp_path / "coach" / "guests" / "7")
            == "coach#u7"
        )

    def test_users_dir_resolves_without_a_user_id(self, registry, tmp_path):
        """Path alone is enough — the dir name carries the identity."""
        assert (
            agent_id_for_working_dir(registry, tmp_path / "coach" / "users" / "99")
            == "coach#u99"
        )

    def test_unknown_dir_still_falls_back(self, registry, tmp_path):
        assert (
            agent_id_for_working_dir(registry, tmp_path / "nope", user_id=1) == "devops"
        )


class TestRouting:
    """apply_route is the path a real message takes."""

    def test_owner_on_personal_profile_is_routed_to_their_own_dir(self, tmp_path):
        from src.bot.orchestration.routing import apply_route

        registry = ProfileRegistry([_profile(tmp_path, personal_memory=True)])
        user_data: dict = {}
        decision = apply_route(
            "log my breakfast",
            user_data,
            registry,
            role="owner",
            user_id=111111111,
        )
        assert decision.working_dir == tmp_path / "coach" / "users" / "111111111"
        assert decision.working_dir.is_dir()

    def test_two_owners_route_to_different_dirs(self, tmp_path):
        from src.bot.orchestration.routing import apply_route

        registry = ProfileRegistry([_profile(tmp_path, personal_memory=True)])
        first = apply_route("hi", {}, registry, role="owner", user_id=111111111)
        second = apply_route("hi", {}, registry, role="owner", user_id=222222222)
        assert first.working_dir != second.working_dir

    def test_shared_profile_is_unaffected(self, tmp_path):
        from src.bot.orchestration.routing import apply_route

        profile = _profile(tmp_path, slug="devops", personal_memory=False)
        registry = ProfileRegistry([profile])
        decision = apply_route("hi", {}, registry, role="owner", user_id=111111111)
        assert decision.working_dir == profile.working_dir


# --- apply_route sticky / one-shot / plain (folded from test_orchestrator_routing) ---


def _route_p(slug, root, default=False):
    return ProfileDefinition(
        slug, slug, Path(root), Path(root) / "m", Path(root) / "p.md", default=default
    )


def _route_reg():
    return ProfileRegistry(
        [_route_p("devops", "/tmp/dev", default=True), _route_p("channel", "/tmp/chan")]
    )


def test_apply_route_sticky_switch_changes_active_and_dir():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("/channel read", user_data, _route_reg())
    assert user_data["active_profile"] == "channel"
    assert decision.working_dir == Path("/tmp/chan")
    assert decision.prompt == "read"
    assert decision.badge.startswith("channel") or "channel" in decision.badge
    assert decision.restore_dir is None


def test_apply_route_one_shot_keeps_active_and_restores():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("@channel hi", user_data, _route_reg())
    assert user_data["active_profile"] == "devops"
    assert decision.working_dir == Path("/tmp/chan")
    assert decision.restore_dir == Path("/tmp/dev")


def test_apply_route_plain_uses_active():
    user_data = {"active_profile": "devops", "current_directory": Path("/tmp/dev")}
    decision = apply_route("hello", user_data, _route_reg())
    assert decision.working_dir == Path("/tmp/dev")
    assert decision.prompt == "hello"
