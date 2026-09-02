"""Tests for denylist path validation."""

from pathlib import Path

from src.security.denylist import (
    DEFAULT_DENIED_PATHS,
    DenylistValidator,
)


class TestDenylistValidator:
    """Test DenylistValidator."""

    def setup_method(self) -> None:
        self.validator = DenylistValidator()

    def test_allows_normal_home_path(self) -> None:
        result, error = self.validator.is_path_allowed("/home/testuser/project/file.py")
        assert result is True
        assert error is None

    def test_blocks_etc_shadow(self) -> None:
        result, error = self.validator.is_path_allowed("/etc/shadow")
        assert result is False
        assert "denied" in error.lower()

    def test_blocks_sudoers(self) -> None:
        result, error = self.validator.is_path_allowed("/etc/sudoers")
        assert result is False

    def test_blocks_sudoers_d_contents(self) -> None:
        result, error = self.validator.is_path_allowed("/etc/sudoers.d/claude-bot")
        assert result is False

    def test_blocks_boot(self) -> None:
        result, error = self.validator.is_path_allowed("/boot/vmlinuz")
        assert result is False

    def test_blocks_proc_kcore(self) -> None:
        result, error = self.validator.is_path_allowed("/proc/kcore")
        assert result is False

    def test_blocks_sys(self) -> None:
        result, error = self.validator.is_path_allowed("/sys/kernel/something")
        assert result is False

    def test_blocks_pem_file(self) -> None:
        result, error = self.validator.is_path_allowed("/home/user/server.pem")
        assert result is False

    def test_blocks_private_key(self) -> None:
        result, error = self.validator.is_path_allowed("/home/user/.ssh/id_rsa")
        assert result is False

    def test_blocks_id_ed25519(self) -> None:
        result, error = self.validator.is_path_allowed("/home/user/.ssh/id_ed25519")
        assert result is False

    def test_blocks_key_file(self) -> None:
        result, error = self.validator.is_path_allowed("/home/user/secret.key")
        assert result is False

    def test_allows_opt_path(self) -> None:
        result, error = self.validator.is_path_allowed("/opt/myapp/config.yaml")
        assert result is True

    def test_allows_var_log(self) -> None:
        result, error = self.validator.is_path_allowed("/var/log/syslog")
        assert result is True

    def test_allows_tmp(self) -> None:
        result, error = self.validator.is_path_allowed("/tmp/scratch.txt")
        assert result is True

    def test_custom_denied_paths(self) -> None:
        validator = DenylistValidator(
            denied_paths=["/secret/"],
            denied_patterns=[],
        )
        result, error = validator.is_path_allowed("/secret/file.txt")
        assert result is False

    def test_custom_denied_patterns(self) -> None:
        validator = DenylistValidator(
            denied_paths=[],
            denied_patterns=["*.secret"],
        )
        result, error = validator.is_path_allowed("/home/user/db.secret")
        assert result is False

    def test_empty_path_denied(self) -> None:
        result, error = self.validator.is_path_allowed("")
        assert result is False

    def test_denies_dotenv_in_project(self) -> None:
        """.env is denied for EVERY profile, devops included (dv-6dbc).

        Reverses the earlier rule ("the agent needs to edit its own config"):
        a leaked token is unrecoverable, and config edits are a human job. See
        docs/DECISIONS.md 2026-08-04.
        """
        result, error = self.validator.is_path_allowed("/home/user/project/.env")
        assert result is False

    def test_denies_dotenv_variants(self) -> None:
        for path in ("/p/.env.local", "/p/.env.bak-144858", "/p/.env.production"):
            result, _ = self.validator.is_path_allowed(path)
            assert result is False, path

    def test_multi_tenant_stores_not_in_global_denylist(self) -> None:
        """bot.db is denied per-profile, not globally.

        devops (full access) keeps DB access -- it is the admin profile and
        answering "how many messages today" is its job. The sandboxed profiles
        are blocked in the can_use_tool callback via TENANT_DATA_PATTERNS.
        """
        result, _ = self.validator.is_path_allowed(
            "/home/testuser/Devvybot/devvy_bot/data/bot.db"
        )
        assert result is True

    def test_denies_credential_files(self) -> None:
        for path in ("/p/auth.json", "/p/openai_credentials.json", "/p/x.token"):
            result, _ = self.validator.is_path_allowed(path)
            assert result is False, path

    def test_still_allows_ordinary_workspace_files(self) -> None:
        """Cross-agent reads stay allowed by design -- don't over-tighten."""
        for path in (
            "/home/testuser/Devvybot/workspaces/channel/notes.md",
            "/home/testuser/Devvybot/devvy_bot/src/bot/orchestrator.py",
            "/home/testuser/Devvybot/workspaces/coach/data.csv",
        ):
            result, _ = self.validator.is_path_allowed(path)
            assert result is True, path

    # --- Home secrets directories (dv-7de7) --------------------------------

    def test_denies_ssh_dir_contents(self) -> None:
        """~/.ssh/ is a denied PREFIX: any file in it, not just id_* names."""
        home = Path.home()
        for name in ("config", "authorized_keys", "known_hosts", "unusual_key"):
            result, error = self.validator.is_path_allowed(str(home / ".ssh" / name))
            assert result is False, name

    def test_denies_aws_dir_contents(self) -> None:
        result, _ = self.validator.is_path_allowed(
            str(Path.home() / ".aws" / "credentials")
        )
        assert result is False

    def test_denies_claude_dir_secrets(self) -> None:
        home = Path.home()
        for name in (".credentials.json", "history.jsonl", "projects/x/y.jsonl"):
            result, _ = self.validator.is_path_allowed(str(home / ".claude" / name))
            assert result is False, name

    def test_claude_internal_dirs_exempt(self) -> None:
        """~/.claude/plans|todos|settings.json stay readable, or the prefix
        denial breaks plan mode on every profile (dv-7de7 §1a)."""
        home = Path.home()
        for name in ("plans/x.md", "todos/t.json", "settings.json"):
            result, error = self.validator.is_path_allowed(str(home / ".claude" / name))
            assert result is True, f"{name}: {error}"

    def test_claude_plugin_dirs_exempt(self) -> None:
        """Plugin/skill code stays readable (the CLI executes it)."""
        result, _ = self.validator.is_path_allowed(
            str(Path.home() / ".claude" / "plugins" / "cache" / "deviz.mjs")
        )
        assert result is True

    def test_denies_shell_history(self) -> None:
        for path in (
            str(Path.home() / ".bash_history"),
            str(Path.home() / ".zsh_history"),
        ):
            result, _ = self.validator.is_path_allowed(path)
            assert result is False, path

    def test_denies_claude_json_account_state(self) -> None:
        for path in (
            str(Path.home() / ".claude.json"),
            str(Path.home() / ".claude.json.backup"),
        ):
            result, _ = self.validator.is_path_allowed(path)
            assert result is False, path


class TestDefaultDeniedPaths:
    """Verify defaults cover critical system paths."""

    def test_defaults_include_shadow(self) -> None:
        assert any("/etc/shadow" in p for p in DEFAULT_DENIED_PATHS)

    def test_defaults_include_sudoers(self) -> None:
        assert any("/etc/sudoers" in p for p in DEFAULT_DENIED_PATHS)

    def test_defaults_include_boot(self) -> None:
        assert any("/boot/" in p for p in DEFAULT_DENIED_PATHS)

    def test_defaults_include_sys(self) -> None:
        assert any("/sys/" in p for p in DEFAULT_DENIED_PATHS)
