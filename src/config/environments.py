"""Environment-specific configuration overrides."""

from typing import Any, Dict


class DevelopmentConfig:
    """Development environment overrides."""

    debug: bool = True
    development_mode: bool = True
    log_level: str = "DEBUG"
    rate_limit_requests: int = 100  # More lenient for testing
    # Idle limit for one SDK turn (see settings.py). Must stay ABOVE the CLI's
    # Bash tool ceiling (BASH_MAX_TIMEOUT_MS, default 600s): a single silent
    # Bash call is the longest gap the CLI can legitimately go quiet for, and
    # at 600/600 the watchdog raced the tool result (2026-09-17 13:43, missed
    # by under a second).
    claude_timeout_seconds: int = 900
    enable_telemetry: bool = False

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }


class TestingConfig:
    """Testing environment configuration."""

    debug: bool = True
    development_mode: bool = True
    database_url: str = "sqlite:///:memory:"
    approved_directory: str = "/tmp/test_projects"
    enable_telemetry: bool = False
    claude_timeout_seconds: int = 30  # Faster timeout for tests
    rate_limit_requests: int = 1000  # No rate limiting in tests

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }


class ProductionConfig:
    """Production environment configuration."""

    debug: bool = False
    development_mode: bool = False
    log_level: str = "INFO"
    enable_telemetry: bool = True
    # Use stricter defaults for production
    claude_max_cost_per_user: float = 5.0  # Lower cost limit
    claude_max_cost_per_request: float = 2.0  # Per-request SDK cap
    rate_limit_requests: int = 5  # Stricter rate limiting
    # No session_timeout_hours override: sessions do not expire on a wall
    # clock at all (docs/DECISIONS.md 2026-09-10). This override used to make
    # production stricter than the 24h default — 12h — which is what actually
    # dropped idle profiles' context in prod while dev/testing looked fine.

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return {
            key: value
            for key, value in cls.__dict__.items()
            if not key.startswith("_")
            and not callable(value)
            and not isinstance(value, classmethod)
        }
