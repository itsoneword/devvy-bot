"""Configuration loading with environment detection."""

import json
import os
from pathlib import Path
from typing import Any, Optional

import structlog
from dotenv import dotenv_values

from src.exceptions import ConfigurationError, InvalidConfigError
from src.utils.agent_env import is_secret_name, scrub_process_env
from src.utils.paths import data_layout

from .environments import DevelopmentConfig, ProductionConfig, TestingConfig
from .settings import Settings

logger = structlog.get_logger()


def load_config(
    env: Optional[str] = None, config_file: Optional[Path] = None
) -> Settings:
    """Load configuration based on environment.

    Args:
        env: Environment name (development, testing, production)
        config_file: Optional path to configuration file

    Returns:
        Configured Settings instance

    Raises:
        ConfigurationError: If configuration is invalid
    """
    # Config .env: non-secret keys go to os.environ (as load_dotenv did, so
    # code and CLIs reading config from the env still see it); secret keys
    # never do. Secrets reach Settings only, via ``_env_file`` (dv-2976).
    env_file = config_file or Path(".env")
    if env_file.exists():
        logger.info("Loading .env file", path=str(env_file))
        _export_config_env(env_file)
    else:
        logger.warning("No .env file found", path=str(env_file))
    secrets_file = secrets_file_for(env_file)
    env_files: tuple = (env_file,)
    if secrets_file is not None:
        if secrets_file.exists():
            _warn_if_loose(secrets_file)
            env_files = (env_file, secrets_file)
            logger.info("Loading secrets file", path=str(secrets_file))
        else:
            logger.warning("No secrets file found", path=str(secrets_file))

    # Determine environment
    env = env or os.getenv("ENVIRONMENT", "development")
    logger.info("Loading configuration", environment=env)

    try:
        # Debug: Log key environment variables before Settings creation
        logger.debug(
            "Environment variables check",
            telegram_bot_token_set=bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            telegram_bot_username=os.getenv("TELEGRAM_BOT_USERNAME"),
            approved_directory=os.getenv("APPROVED_DIRECTORY"),
            debug_mode=os.getenv("DEBUG"),
        )

        # Load base settings from environment variables
        # pydantic-settings will automatically read from environment variables
        # Later files win: the secrets file overrides the config .env.
        settings = Settings(_env_file=env_files)  # type: ignore[call-arg]

        # A reports chat ID is non-secret. Allow an install to set it in the
        # data directory without giving an agent access to the protected .env.
        if settings.reports_chat_id is None and settings.devvy_data_root is not None:
            reports_id_file = settings.devvy_data_root / "reports_chat_id"
            try:
                reports_id = reports_id_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Could not read reports chat ID", error=str(e))
            else:
                try:
                    settings.reports_chat_id = int(reports_id)
                except ValueError:
                    logger.warning(
                        "Invalid reports chat ID file", path=str(reports_id_file)
                    )

        _load_reports_inbound_file(settings)

        # Apply environment-specific overrides
        settings = _apply_environment_overrides(settings, env)

        # Validate configuration
        _validate_config(settings)

        logger.info(
            "Configuration loaded successfully",
            environment=env,
            debug=settings.debug,
            approved_directory=str(settings.approved_directory),
            features_enabled=_get_enabled_features_summary(settings),
        )

        return settings

    except Exception as e:
        logger.error("Failed to load configuration", error=str(e), environment=env)
        raise ConfigurationError(f"Configuration loading failed: {e}") from e


def secrets_file_for(env_file: Path) -> Optional[Path]:
    """The secrets file for this deployment, or None (legacy: .env holds them).

    ``DEVVY_DATA_ROOT`` from the process env, else from ``env_file``.
    """
    return data_layout(dotenv=env_file).bot_secrets_file


def secret_env_names(settings: Settings, env_file: Optional[Path] = None) -> set:
    """Names to scrub from os.environ: Settings' secret fields and every key
    of the secrets file (all of it is secret by definition)."""
    names = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation) or is_secret_name(name)
    }
    secrets_file = secrets_file_for(env_file or Path(".env"))
    if secrets_file is not None and secrets_file.exists():
        names |= {k for k in dotenv_values(secrets_file) if k}
    return names


def scrub_secrets_from_environ(
    settings: Settings, env_file: Optional[Path] = None
) -> list:
    """Drop bot secrets from os.environ once Settings holds them (dv-2976).

    Every child the bot spawns (Claude CLI via the SDK, codex, evaluator,
    bgjob) inherits os.environ, so after this none of them gets a secret the
    bot did not hand it explicitly. Returns the removed names (never values).
    """
    removed = scrub_process_env(secret_env_names(settings, env_file))
    logger.info("Scrubbed secrets from process env", names=removed)
    return removed


def _export_config_env(env_file: Path) -> None:
    """load_dotenv(env_file) minus secrets: set unset non-secret keys only."""
    for key, value in dotenv_values(env_file).items():
        if not key or value is None or is_secret_name(key):
            continue
        os.environ.setdefault(key, value)


def _warn_if_loose(path: Path) -> None:
    """A secrets file readable by group/others is a misconfiguration."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Secrets file is group/world accessible; chmod 600 it",
            path=str(path),
            mode=oct(mode),
        )


def _apply_environment_overrides(settings: Settings, env: Optional[str]) -> Settings:
    """Apply environment-specific configuration overrides."""
    overrides = {}

    if env == "development":
        overrides = DevelopmentConfig.as_dict()
    elif env == "testing":
        overrides = TestingConfig.as_dict()
    elif env == "production":
        overrides = ProductionConfig.as_dict()
    else:
        logger.warning("Unknown environment, using default settings", environment=env)

    # Apply overrides
    for key, value in overrides.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
            logger.debug(
                "Applied environment override", key=key, value=value, environment=env
            )

    return settings


def _validate_config(settings: Settings) -> None:
    """Perform additional runtime validation."""
    # Check file system permissions (allowlist mode only)
    if settings.approved_directory is not None:
        try:
            if not os.access(settings.approved_directory, os.R_OK | os.X_OK):
                raise InvalidConfigError(
                    f"Cannot access approved directory: "
                    f"{settings.approved_directory}"
                )
        except OSError as e:
            raise InvalidConfigError(f"Error accessing approved directory: {e}") from e

    # Validate feature dependencies
    if settings.enable_mcp and not settings.mcp_config_path:
        raise InvalidConfigError("MCP enabled but no config path provided")

    if settings.enable_token_auth and not settings.auth_token_secret:
        raise InvalidConfigError("Token auth enabled but no secret provided")

    # Validate database path for SQLite
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_path
        if db_path:
            # Ensure parent directory exists
            db_path.parent.mkdir(parents=True, exist_ok=True)

    # Validate rate limiting settings
    if settings.rate_limit_requests <= 0:
        raise InvalidConfigError("rate_limit_requests must be positive")

    if settings.rate_limit_window <= 0:
        raise InvalidConfigError("rate_limit_window must be positive")

    if settings.claude_timeout_seconds <= 0:
        raise InvalidConfigError("claude_timeout_seconds must be positive")

    # Validate cost limits
    if settings.claude_max_cost_per_user <= 0:
        raise InvalidConfigError("claude_max_cost_per_user must be positive")

    if settings.claude_max_cost_per_user_guest <= 0:
        raise InvalidConfigError("claude_max_cost_per_user_guest must be positive")

    if settings.claude_max_cost_per_request <= 0:
        raise InvalidConfigError("claude_max_cost_per_request must be positive")


def _get_enabled_features_summary(settings: Settings) -> list[str]:
    """Get a summary of enabled features for logging."""
    features = []
    if settings.enable_mcp:
        features.append("mcp")
    if settings.enable_file_uploads:
        features.append("file_uploads")
    if settings.enable_token_auth:
        features.append("token_auth")
    if settings.webhook_url:
        features.append("webhook")
    return features


def create_test_config(**overrides: Any) -> Settings:
    """Create configuration for testing with optional overrides.

    Args:
        **overrides: Configuration values to override

    Returns:
        Settings instance configured for testing
    """
    # Start with testing defaults
    test_values = TestingConfig.as_dict()

    # Add required fields for testing
    test_values.update(
        {
            "telegram_bot_token": "test_token_123",
            "telegram_bot_username": "test_bot",
            "approved_directory": "/tmp/test_projects",
        }
    )

    # Apply any overrides
    test_values.update(overrides)

    # Ensure test directory exists
    test_dir = Path(test_values["approved_directory"])
    test_dir.mkdir(parents=True, exist_ok=True)

    # Create settings with test values
    settings = Settings(**test_values)

    return settings


def _load_reports_inbound_file(settings: Settings) -> None:
    """Fill reports bindings / inbound policy from ``<data-root>/reports_bindings.json``.

    Format: ``{"policy": "owner", "bindings": {"Topic": "profile-slug"}}``.
    Environment settings (REPORTS_BINDINGS / REPORTS_INBOUND_POLICY) win; the
    file only supplies what the environment left at its default.
    """
    if settings.devvy_data_root is None:
        return
    path = settings.devvy_data_root / "reports_bindings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        logger.warning("Could not read reports bindings", path=str(path), error=str(e))
        return
    if not isinstance(data, dict):
        logger.warning("Invalid reports bindings file", path=str(path))
        return
    bindings = data.get("bindings")
    if not settings.reports_bindings and isinstance(bindings, dict):
        settings.reports_bindings = {
            str(k).strip(): str(v).strip().lower()
            for k, v in bindings.items()
            if str(k).strip() and str(v).strip()
        }
    reply_only = data.get("reply_only")
    if not settings.reports_reply_only and isinstance(reply_only, list):
        settings.reports_reply_only = [
            str(t).strip() for t in reply_only if str(t).strip()
        ]
    policy = data.get("policy")
    if "reports_inbound_policy" not in settings.model_fields_set and isinstance(
        policy, str
    ):
        settings.reports_inbound_policy = policy.strip().lower()
