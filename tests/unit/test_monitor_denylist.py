"""Tests for bash boundary checking in denylist mode."""

from pathlib import Path

import pytest

from src.claude.monitor import (
    check_bash_denied_paths,
    check_bash_directory_boundary,
    check_bash_workspace_writes,
)


class TestBashBoundaryDenylistMode:
    """When approved_directory is None, use denylist validation."""

    def test_allows_normal_command(self) -> None:
        valid, error = check_bash_directory_boundary(
            "ls /home/testuser/project",
            working_directory=Path("/home/testuser/project"),
            approved_directory=None,
        )
        assert valid is True

    def test_allows_mkdir_in_home(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir /home/testuser/newproject",
            working_directory=Path("/home/testuser"),
            approved_directory=None,
        )
        assert valid is True

    def test_blocks_rm_etc_shadow(self) -> None:
        valid, error = check_bash_directory_boundary(
            "rm /etc/shadow",
            working_directory=Path("/home/testuser"),
            approved_directory=None,
        )
        assert valid is False

    def test_blocks_cp_to_boot(self) -> None:
        valid, error = check_bash_directory_boundary(
            "cp malware /boot/",
            working_directory=Path("/home/testuser"),
            approved_directory=None,
        )
        assert valid is False

    def test_read_only_commands_always_pass(self) -> None:
        valid, error = check_bash_directory_boundary(
            "cat /etc/passwd",
            working_directory=Path("/home/testuser"),
            approved_directory=None,
        )
        assert valid is True

    def test_allowlist_mode_unchanged(self, tmp_path: Path) -> None:
        """When approved_directory is set, existing allowlist behavior works."""
        valid, error = check_bash_directory_boundary(
            "rm /etc/passwd",
            working_directory=tmp_path,
            approved_directory=tmp_path,
        )
        assert valid is False


class TestBashWorkspaceWrites:
    """dv-6dbc: bash writes stay in the workspace on sandboxed profiles.

    Mostly defence in depth (the CLI sandbox makes the FS read-only outside the
    workspace) -- except for sandbox_excluded_commands, which run outside the
    bash sandbox entirely.
    """

    WS = Path("/home/testuser/Devvybot/workspaces/general")

    def test_allows_redirect_into_workspace(self) -> None:
        ok, _ = check_bash_workspace_writes("echo hi > out.txt", self.WS)
        assert ok is True

    def test_denies_redirect_outside_workspace(self) -> None:
        ok, err = check_bash_workspace_writes(
            "echo probe > /home/testuser/PROBE.txt", self.WS
        )
        assert ok is False
        assert "outside this agent's workspace" in (err or "")

    def test_denies_attached_redirect_form(self) -> None:
        ok, _ = check_bash_workspace_writes("echo probe >/home/testuser/x.txt", self.WS)
        assert ok is False

    def test_denies_append_outside_workspace(self) -> None:
        ok, _ = check_bash_workspace_writes(
            "cat notes.md >> /home/testuser/steal.md", self.WS
        )
        assert ok is False

    def test_denies_cp_outside_workspace(self) -> None:
        ok, _ = check_bash_workspace_writes(
            "cp notes.md /home/testuser/steal.md", self.WS
        )
        assert ok is False

    def test_allows_scratch_root(self) -> None:
        ok, _ = check_bash_workspace_writes(
            "echo hi > /tmp/claude-1000/chart.png", self.WS, [Path("/tmp/claude-1000")]
        )
        assert ok is True

    def test_allows_ordinary_read_commands(self) -> None:
        for cmd in (
            "ls -la",
            "git status --short",
            "grep -rn TODO .",
            "python3 analyze.py --out results.csv",
            "cat /home/testuser/Devvybot/workspaces/channel/notes.md",
        ):
            ok, err = check_bash_workspace_writes(cmd, self.WS)
            assert ok is True, f"{cmd}: {err}"

    def test_allows_mkdir_in_workspace(self) -> None:
        ok, _ = check_bash_workspace_writes("mkdir -p data/out", self.WS)
        assert ok is True


class TestDeniedPathsFalsePositives:
    """dv-6dbc: the substring scan must not eat ordinary commands.

    `.env` is a substring of `os.environ` and `credentials` is an English word,
    so a raw substring match denied `python3 -c "...os.environ[...]"` and
    `grep -rn credentials src/` -- both things devops runs constantly.
    """

    REPO = Path("/home/testuser/Devvybot/devvy_bot")

    @pytest.mark.parametrize(
        "command",
        [
            "python3 -c \"import os; print(os.environ['HOME'])\"",
            "python3 -c 'import os; print(os.environ)'",
            "grep -rn credentials src/",
            "poetry run pytest tests/unit/test_credentials.py",
            "ls -la | grep env",
            "git log --oneline --grep=token",
            "poetry run pytest tests/unit -q",
            "journalctl -u claude-bot --since -5m",
            "cat src/config/settings.py",
        ],
    )
    def test_ordinary_commands_allowed(self, command: str) -> None:
        ok, err = check_bash_denied_paths(command, self.REPO, full_access=True)
        assert ok is True, f"{command}: {err}"

    @pytest.mark.parametrize(
        "command",
        [
            "cat /home/testuser/Devvybot/devvy_bot/.env",
            "head -1 .env",
            "cat .env.local",
            "cat openai_credentials.json",
            "cat ~/.ssh/id_rsa",
        ],
    )
    def test_secrets_denied_even_for_full_access(self, command: str) -> None:
        ok, _ = check_bash_denied_paths(command, self.REPO, full_access=True)
        assert ok is False, command

    @pytest.mark.parametrize(
        "command",
        [
            "sqlite3 data/bot.db .tables",
            "python3 -c \"import sqlite3;c=sqlite3.connect('/home/x/data/bot.db')\"",
        ],
    )
    def test_tenant_stores_denied_for_sandboxed_only(self, command: str) -> None:
        denied, _ = check_bash_denied_paths(command, self.REPO, full_access=False)
        assert denied is False, command
        allowed, _ = check_bash_denied_paths(command, self.REPO, full_access=True)
        assert allowed is True, f"devops must keep DB access: {command}"


class TestBashReadScope:
    """dv-7de7: guest read scope enforcement inside check_bash_denied_paths.

    ``read_scope=None`` (every pre-existing caller) keeps prior behavior;
    owner scopes no-op; guest scopes add cross-user denial, home confinement
    and the search-ancestor rule for recursive readers.
    """

    GUEST_ID = 222
    OTHER_ID = 111

    def _registry(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        from src.profiles.registry import ProfileDefinition, ProfileRegistry

        ws = tmp_path / "workspaces"
        profiles = []
        for slug, roles in [
            ("devops", {"owner"}),
            ("channel", {"owner"}),
            ("chef", {"owner", "guest"}),
            ("coach", {"owner", "guest"}),
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

    def _guest(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        from src.security.read_scope import build_read_scope

        registry = self._registry(tmp_path)
        coach = registry.get_by_slug("coach")
        cwd = coach.working_dir / "guests" / str(self.GUEST_ID)
        cwd.mkdir(parents=True, exist_ok=True)
        scope = build_read_scope(
            registry=registry,
            working_directory=cwd,
            user_id=self.GUEST_ID,
            role="guest",
        )
        return registry, cwd, scope

    def test_ssh_dir_denied_even_without_scope(self, tmp_path: Path) -> None:
        """The ~/.ssh/ prefix denial is role-independent (plain denylist)."""
        ok, _ = check_bash_denied_paths("cat ~/.ssh/config", tmp_path, full_access=True)
        assert ok is False
        ok, _ = check_bash_denied_paths(
            "cat ~/.ssh/authorized_keys", tmp_path, full_access=False
        )
        assert ok is False

    def test_cross_user_denied_with_scope_allowed_without(self, tmp_path: Path) -> None:
        registry, cwd, scope = self._guest(tmp_path)
        coach = registry.get_by_slug("coach")
        other = coach.working_dir / "users" / str(self.OTHER_ID) / "food.md"
        command = f"cat {other}"
        # Back-compat: no scope -> only the denylist applies.
        ok, _ = check_bash_denied_paths(command, cwd)
        assert ok is True
        # Guest scope: cross-user tree denied.
        ok, error = check_bash_denied_paths(command, cwd, read_scope=scope)
        assert ok is False
        assert "another user" in (error or "")

    def test_recursive_grep_over_profile_root_denied(self, tmp_path: Path) -> None:
        registry, cwd, scope = self._guest(tmp_path)
        chef = registry.get_by_slug("chef")
        ok, error = check_bash_denied_paths(
            f"grep -r foo {chef.working_dir}", cwd, read_scope=scope
        )
        assert ok is False
        assert "narrower" in (error or "")

    def test_narrow_grep_allowed(self, tmp_path: Path) -> None:
        registry, cwd, scope = self._guest(tmp_path)
        chef = registry.get_by_slug("chef")
        target = chef.working_dir / "recipes" / "a.md"
        ok, error = check_bash_denied_paths(f"grep foo {target}", cwd, read_scope=scope)
        assert ok is True, error

    def test_find_over_workspaces_denied(self, tmp_path: Path) -> None:
        _registry, cwd, scope = self._guest(tmp_path)
        ok, _ = check_bash_denied_paths(
            f"find {tmp_path / 'workspaces'} -name '*.md'", cwd, read_scope=scope
        )
        assert ok is False

    def test_glob_prefix_checked(self, tmp_path: Path) -> None:
        registry, cwd, scope = self._guest(tmp_path)
        coach = registry.get_by_slug("coach")
        ok, _ = check_bash_denied_paths(
            f"grep foo {coach.working_dir / 'users'}/*", cwd, read_scope=scope
        )
        assert ok is False

    def test_guest_home_read_denied_system_read_allowed(self, tmp_path: Path) -> None:
        _registry, cwd, scope = self._guest(tmp_path)
        ok, _ = check_bash_denied_paths(
            f"cat {Path.home() / 'other-repo' / 'notes.txt'}",
            cwd,
            read_scope=scope,
        )
        assert ok is False
        ok, error = check_bash_denied_paths("cat /etc/hostname", cwd, read_scope=scope)
        assert ok is True, error

    def test_guest_own_dir_allowed(self, tmp_path: Path) -> None:
        _registry, cwd, scope = self._guest(tmp_path)
        ok, error = check_bash_denied_paths(
            f"cat {cwd / 'notes.md'}", cwd, read_scope=scope
        )
        assert ok is True, error

    def test_owner_scope_is_noop(self, tmp_path: Path) -> None:
        from src.security.read_scope import build_read_scope

        registry = self._registry(tmp_path)
        devops = registry.get_by_slug("devops")
        chef = registry.get_by_slug("chef")
        coach = registry.get_by_slug("coach")
        scope = build_read_scope(
            registry=registry,
            working_directory=devops.working_dir,
            user_id=1,
            role="owner",
        )
        for command in (
            f"grep -r foo {chef.working_dir}",
            f"cat {coach.working_dir / 'users' / '999' / 'x.md'}",
            f"cat {Path.home() / 'somewhere' / 'else.txt'}",
        ):
            ok, error = check_bash_denied_paths(
                command, devops.working_dir, full_access=True, read_scope=scope
            )
            assert ok is True, f"{command}: {error}"
