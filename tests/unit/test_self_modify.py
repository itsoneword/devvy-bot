"""Tests for self-modification guard."""

from pathlib import Path

from src.bot.features.self_modify import SelfModificationGuard


class TestSelfModificationGuard:
    """Test file tier classification."""

    def setup_method(self) -> None:
        self.guard = SelfModificationGuard(
            bot_root=Path("/home/testuser/claude-code-telegram"),
        )

    def test_soul_file_is_tier1(self) -> None:
        assert (
            self.guard.get_tier("/home/testuser/claude-code-telegram/data/soul.md") == 1
        )

    def test_memory_file_is_tier1(self) -> None:
        assert (
            self.guard.get_tier(
                "/home/testuser/claude-code-telegram/data/memory/user.md"
            )
            == 1
        )

    def test_claude_md_is_tier1(self) -> None:
        assert self.guard.get_tier("/home/testuser/claude-code-telegram/CLAUDE.md") == 1

    def test_source_code_is_tier2(self) -> None:
        assert (
            self.guard.get_tier("/home/testuser/claude-code-telegram/src/main.py") == 2
        )

    def test_pyproject_is_tier2(self) -> None:
        assert (
            self.guard.get_tier("/home/testuser/claude-code-telegram/pyproject.toml")
            == 2
        )

    def test_unrelated_file_is_tier0(self) -> None:
        """Files outside bot_root are not self-modification at all."""
        assert self.guard.get_tier("/home/testuser/other-project/main.py") == 0

    def test_tier1_needs_no_approval(self) -> None:
        assert (
            self.guard.needs_approval(
                "/home/testuser/claude-code-telegram/data/soul.md"
            )
            is False
        )

    def test_tier2_needs_approval(self) -> None:
        assert (
            self.guard.needs_approval("/home/testuser/claude-code-telegram/src/main.py")
            is True
        )

    def test_tier0_needs_no_approval(self) -> None:
        assert self.guard.needs_approval("/home/testuser/other/file.py") is False

    def test_needs_restart_for_source(self) -> None:
        assert (
            self.guard.needs_restart("/home/testuser/claude-code-telegram/src/main.py")
            is True
        )

    def test_no_restart_for_soul(self) -> None:
        assert (
            self.guard.needs_restart("/home/testuser/claude-code-telegram/data/soul.md")
            is False
        )

    def test_no_restart_for_external(self) -> None:
        assert self.guard.needs_restart("/home/testuser/other/file.py") is False

    def test_restart_for_pyproject(self) -> None:
        assert (
            self.guard.needs_restart(
                "/home/testuser/claude-code-telegram/pyproject.toml"
            )
            is True
        )
