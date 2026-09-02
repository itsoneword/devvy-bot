"""Tests for the per-session read scope (dv-7de7).

Guests get a read allowlist + cross-user tree denial + search-ancestor rule.
Owners build a no-op scope (``guest_allowlist is None``) and keep today's
unrestricted cross-agent reads.
"""

from pathlib import Path
from typing import Optional

from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.security.read_scope import (
    ReadScope,
    _is_claude_internal_path,
    build_read_scope,
    home_denied_dirs,
)

GUEST_ID = 222
OTHER_GUEST_ID = 333
OTHER_OWNER_ID = 111


def make_registry(tmp_path: Path) -> ProfileRegistry:
    """Registry mirroring the live shape: devops+channel owner-only,
    general/chef/coach/pm guest-reachable."""
    ws = tmp_path / "workspaces"
    profiles = []
    for slug, roles in [
        ("devops", {"owner"}),
        ("channel", {"owner"}),
        ("general", {"owner", "guest"}),
        ("chef", {"owner", "guest"}),
        ("coach", {"owner", "guest"}),
        ("pm", {"owner", "guest"}),
    ]:
        root = ws / slug
        root.mkdir(parents=True, exist_ok=True)
        profiles.append(
            ProfileDefinition(
                slug=slug,
                name=slug,
                working_dir=root,
                memory_dir=root / "memory",
                persona=root / "persona.md",
                default=(slug == "devops"),
                allowed_roles=frozenset(roles),
            )
        )
    return ProfileRegistry(profiles)


def guest_scope(
    tmp_path: Path,
    registry: Optional[ProfileRegistry] = None,
    slug: str = "coach",
) -> ReadScope:
    registry = registry or make_registry(tmp_path)
    profile = registry.get_by_slug(slug)
    assert profile is not None
    cwd = profile.working_dir / "guests" / str(GUEST_ID)
    cwd.mkdir(parents=True, exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return build_read_scope(
        registry=registry,
        working_directory=cwd,
        user_id=GUEST_ID,
        role="guest",
        scratch_roots=[scratch],
    )


class TestGuestCheckRead:
    def test_own_guest_dir_allowed(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        target = scope.working_directory / "notes.md"
        ok, _ = scope.check_read(str(target))
        assert ok is True

    def test_own_users_dir_on_other_profile_allowed(self, tmp_path: Path) -> None:
        """Both shapes of the user's OWN dirs are exempt (role-change safe)."""
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        chef = registry.get_by_slug("chef")
        assert chef is not None
        ok, _ = scope.check_read(
            str(chef.working_dir / "users" / str(GUEST_ID) / "x.md")
        )
        assert ok is True

    def test_other_guest_dir_denied(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        ok, error = scope.check_read(
            str(coach.working_dir / "guests" / str(OTHER_GUEST_ID) / "x.md")
        )
        assert ok is False
        assert "another user" in (error or "")

    def test_other_users_dir_denied(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        ok, error = scope.check_read(
            str(coach.working_dir / "users" / str(OTHER_OWNER_ID) / "food.md")
        )
        assert ok is False
        assert "another user" in (error or "")

    def test_guest_reachable_roots_allowed(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        for slug in ("general", "chef", "coach", "pm"):
            profile = registry.get_by_slug(slug)
            assert profile is not None
            ok, error = scope.check_read(
                str(profile.working_dir / "shared" / "file.md")
            )
            assert ok is True, f"{slug}: {error}"

    def test_owner_only_channel_root_denied(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        channel = registry.get_by_slug("channel")
        assert channel is not None
        ok, error = scope.check_read(str(channel.working_dir / "persona.md"))
        assert ok is False
        assert "readable scope" in (error or "")

    def test_repo_and_home_paths_denied(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        for target in (
            str(Path.home() / "Devvybot" / "devvy_bot" / "config" / "x.yaml"),
            str(Path.home() / "residence_permit.pdf"),
            "/etc/passwd",
        ):
            ok, _ = scope.check_read(target)
            assert ok is False, target

    def test_scratch_root_allowed(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_read(str(tmp_path / "scratch" / "img.png"))
        assert ok is True

    def test_claude_internal_allowed(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_read(str(Path.home() / ".claude" / "plans" / "p.md"))
        assert ok is True

    def test_relative_path_resolved_against_cwd(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_read("notes/today.md")
        assert ok is True
        ok, _ = scope.check_read(f"../{OTHER_GUEST_ID}/x.md")
        assert ok is False


class TestOwnerScopeIsNoOp:
    def test_owner_allowlist_is_none(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        devops = registry.get_by_slug("devops")
        assert devops is not None
        scope = build_read_scope(
            registry=registry,
            working_directory=devops.working_dir,
            user_id=OTHER_OWNER_ID,
            role="owner",
        )
        assert scope.guest_allowlist is None
        assert scope.is_guest is False

    def test_owner_cross_agent_read_allowed(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        general = registry.get_by_slug("general")
        channel = registry.get_by_slug("channel")
        assert general is not None and channel is not None
        scope = build_read_scope(
            registry=registry,
            working_directory=general.working_dir,
            user_id=OTHER_OWNER_ID,
            role="owner",
        )
        ok, _ = scope.check_read(str(channel.working_dir / "persona.md"))
        assert ok is True

    def test_owner_cross_user_tree_read_allowed(self, tmp_path: Path) -> None:
        """Amendment 2026-08-08: cross-user denial is guest-only; owners
        (incl. devops) keep unrestricted reads."""
        registry = make_registry(tmp_path)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        scope = build_read_scope(
            registry=registry,
            working_directory=registry.get_default().working_dir,
            user_id=OTHER_OWNER_ID,
            role="owner",
        )
        ok, _ = scope.check_read(
            str(coach.working_dir / "users" / str(GUEST_ID) / "x.md")
        )
        assert ok is True

    def test_owner_search_root_unrestricted(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        chef = registry.get_by_slug("chef")
        assert chef is not None
        scope = build_read_scope(
            registry=registry,
            working_directory=registry.get_default().working_dir,
            role="owner",
        )
        ok, _ = scope.check_search_root(str(chef.working_dir))
        assert ok is True

    def test_owner_bash_read_unrestricted(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = build_read_scope(
            registry=registry,
            working_directory=registry.get_default().working_dir,
            role="owner",
        )
        ok, _ = scope.check_bash_read(Path.home() / "some-repo" / "file.py")
        assert ok is True


class TestGuestDetection:
    def test_owner_on_users_dir_not_guest(self, tmp_path: Path) -> None:
        """users/<id> parent means owner-on-personal-memory, NOT guest."""
        registry = make_registry(tmp_path)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        cwd = coach.working_dir / "users" / str(OTHER_OWNER_ID)
        cwd.mkdir(parents=True, exist_ok=True)
        scope = build_read_scope(
            registry=registry,
            working_directory=cwd,
            user_id=OTHER_OWNER_ID,
            role="owner",
        )
        assert scope.is_guest is False

    def test_role_none_in_guests_cwd_is_guest(self, tmp_path: Path) -> None:
        """Webhook/scheduler callers pass role=None; a guests/ cwd fails
        closed to guest."""
        registry = make_registry(tmp_path)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        cwd = coach.working_dir / "guests" / str(GUEST_ID)
        cwd.mkdir(parents=True, exist_ok=True)
        scope = build_read_scope(
            registry=registry,
            working_directory=cwd,
            user_id=None,
            role=None,
        )
        assert scope.is_guest is True
        # The uid is recovered from the cwd, so own dirs still work.
        ok, _ = scope.check_read(str(cwd / "notes.md"))
        assert ok is True

    def test_guest_role_without_registry_fails_closed(self, tmp_path: Path) -> None:
        cwd = tmp_path / "somewhere"
        cwd.mkdir()
        scope = build_read_scope(
            registry=None,
            working_directory=cwd,
            user_id=GUEST_ID,
            role="guest",
        )
        assert scope.is_guest is True
        ok, _ = scope.check_read(str(cwd / "x.md"))
        assert ok is True
        ok, _ = scope.check_read(str(Path.home() / "elsewhere.md"))
        assert ok is False

    def test_role_none_owner_cwd_is_owner(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = build_read_scope(
            registry=registry,
            working_directory=registry.get_default().working_dir,
            role=None,
        )
        assert scope.is_guest is False


class TestSearchRoot:
    def test_guest_profile_root_denied_as_ancestor(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        chef = registry.get_by_slug("chef")
        assert chef is not None
        ok, error = scope.check_search_root(str(chef.working_dir))
        assert ok is False
        assert "narrower" in (error or "")

    def test_guest_workspaces_parent_denied(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        ok, _ = scope.check_search_root(str(tmp_path / "workspaces"))
        assert ok is False

    def test_guest_narrow_subdir_allowed(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        chef = registry.get_by_slug("chef")
        assert chef is not None
        ok, error = scope.check_search_root(str(chef.working_dir / "recipes"))
        assert ok is True, error

    def test_guest_own_dir_allowed(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_search_root(str(scope.working_directory))
        assert ok is True

    def test_guest_home_denied_as_ancestor_of_secrets(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_search_root(str(Path.home()))
        assert ok is False

    def test_guest_secrets_dir_itself_denied(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, _ = scope.check_search_root(str(Path.home() / ".ssh"))
        assert ok is False


class TestBashRead:
    def test_guest_system_paths_allowed(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        for target in ("/etc/hostname", "/usr/bin/python3", "/tmp/x.txt"):
            ok, _ = scope.check_bash_read(Path(target))
            assert ok is True, target

    def test_guest_home_paths_denied(self, tmp_path: Path) -> None:
        scope = guest_scope(tmp_path)
        ok, error = scope.check_bash_read(Path.home() / "other-repo" / "file.py")
        assert ok is False
        assert "readable scope" in (error or "")

    def test_guest_cross_user_tree_denied(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        ok, error = scope.check_bash_read(
            coach.working_dir / "users" / str(OTHER_OWNER_ID) / "x.md"
        )
        assert ok is False
        assert "another user" in (error or "")

    def test_guest_own_and_shared_dirs_allowed(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path)
        scope = guest_scope(tmp_path, registry)
        chef = registry.get_by_slug("chef")
        assert chef is not None
        ok, _ = scope.check_bash_read(scope.working_directory / "a.md")
        assert ok is True
        # Shared profile roots are in the allowlist even when under $HOME.
        if str(chef.working_dir).startswith(str(Path.home())):
            ok, _ = scope.check_bash_read(chef.working_dir / "recipes" / "a.md")
            assert ok is True


class TestClaudeInternalPath:
    def test_internal_subdirs(self) -> None:
        home = Path.home()
        assert _is_claude_internal_path(str(home / ".claude" / "plans" / "x.md"))
        assert _is_claude_internal_path(str(home / ".claude" / "todos" / "t"))
        assert _is_claude_internal_path(str(home / ".claude" / "settings.json"))

    def test_non_internal_denied(self) -> None:
        home = Path.home()
        assert not _is_claude_internal_path(str(home / ".claude" / ".credentials.json"))
        assert not _is_claude_internal_path(str(home / ".claude" / "projects" / "x"))
        assert not _is_claude_internal_path(str(home / ".claude" / "history.jsonl"))

    def test_readonly_dirs_only_with_flag(self) -> None:
        plugin = str(Path.home() / ".claude" / "plugins" / "cache" / "x.mjs")
        assert not _is_claude_internal_path(plugin)
        assert _is_claude_internal_path(plugin, include_readonly_dirs=True)


class TestHomeDeniedDirs:
    def test_covers_the_three_prefixes(self) -> None:
        names = {p.name for p in home_denied_dirs()}
        assert names == {".ssh", ".aws", ".claude"}
