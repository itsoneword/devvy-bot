"""Tests for SecurityValidator in denylist mode."""

import tempfile
from pathlib import Path

from src.security.validators import SecurityValidator


class TestSecurityValidatorDenylistMode:
    """SecurityValidator with approved_directory=None uses denylist."""

    def setup_method(self) -> None:
        self.validator = SecurityValidator(
            approved_directory=None,
            disable_security_patterns=False,
        )

    def test_allows_arbitrary_home_path(self) -> None:
        valid, resolved, error = self.validator.validate_path(
            "/home/testuser/any-project/file.py"
        )
        assert valid is True
        assert error is None

    def test_blocks_etc_shadow(self) -> None:
        valid, resolved, error = self.validator.validate_path("/etc/shadow")
        assert valid is False
        assert "denied" in error.lower()

    def test_blocks_pem_file(self) -> None:
        valid, resolved, error = self.validator.validate_path("/home/user/cert.pem")
        assert valid is False

    def test_blocks_ssh_key(self) -> None:
        valid, resolved, error = self.validator.validate_path("/home/user/.ssh/id_rsa")
        assert valid is False

    def test_dangerous_patterns_still_checked(self) -> None:
        """Shell metacharacters should still be blocked."""
        valid, resolved, error = self.validator.validate_path("$(rm -rf /)")
        assert valid is False

    def test_relative_path_resolved_against_current_dir(self) -> None:
        valid, resolved, error = self.validator.validate_path(
            "subdir/file.py",
            current_dir=Path("/home/testuser/project"),
        )
        assert valid is True
        assert resolved == Path("/home/testuser/project/subdir/file.py").resolve()


class TestSecurityValidatorAllowlistModeUnchanged:
    """Existing allowlist behavior must be preserved."""

    def setup_method(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.validator = SecurityValidator(
            approved_directory=self.tmp,
            disable_security_patterns=False,
        )

    def test_allows_path_inside_approved(self) -> None:
        valid, resolved, error = self.validator.validate_path("file.py")
        assert valid is True

    def test_blocks_path_outside_approved(self) -> None:
        valid, resolved, error = self.validator.validate_path("/etc/passwd")
        assert valid is False
