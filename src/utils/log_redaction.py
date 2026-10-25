"""Redaction of secrets from log records.

httpx logs the full request URL at INFO level, and every Telegram Bot API call
embeds the bot token in its path::

    HTTP Request: POST https://api.telegram.org/bot<token>/getUpdates "HTTP/1.1 200 OK"

That put the live bot token in journald in plaintext on every poll. The filter
below is installed on the root logging handlers, so it covers httpx and any
other library that happens to log a URL containing a credential.
"""

import logging
import re
from typing import Any, List, Pattern

#: Telegram bot tokens look like ``123456789:AA...`` and appear in API paths.
#: File downloads (``/file/bot<token>/...``) URL-encode the colon as ``%3A``.
_TELEGRAM_TOKEN: Pattern[str] = re.compile(
    r"(?<=/bot)\d{5,}(?::|%3[Aa])[A-Za-z0-9_-]{20,}"
)

#: Generic ``?key=...``/``&token=...`` style query credentials.
_QUERY_SECRET: Pattern[str] = re.compile(
    r"([?&](?:api_?key|access_?token|token|secret|password)=)[^&\s\"']+",
    re.IGNORECASE,
)

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Strip known credential shapes out of ``text``."""
    text = _TELEGRAM_TOKEN.sub(REDACTED, text)
    text = _QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    return text


# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


class SecretRedactingFilter(logging.Filter):
    """Rewrite log records in place, removing credentials from the message.

    Implemented as a filter (rather than a formatter) so it applies regardless
    of which renderer structlog is configured with, and so records that are
    passed on to other handlers are already clean.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, leave as-is
            return True

        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None

        if record.args:
            record.args = _redact_args(record.args)

        return True


def _redact_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(redact(a) if isinstance(a, str) else a for a in args)
    return args


def install_secret_redaction() -> None:
    """Attach :class:`SecretRedactingFilter` to every root logging handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
