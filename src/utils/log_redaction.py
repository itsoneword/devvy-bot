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
from typing import Any, Pattern

#: Telegram bot tokens look like ``123456789:AA...`` and appear in API paths.
_TELEGRAM_TOKEN: Pattern[str] = re.compile(r"(?<=/bot)\d{5,}:[A-Za-z0-9_-]{20,}")

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
