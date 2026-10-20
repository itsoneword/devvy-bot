"""Secrets must never reach the log stream (journald)."""

import logging

from src.utils.log_redaction import (
    REDACTED,
    SecretRedactingFilter,
    install_secret_redaction,
    redact,
)

# Synthetic. Must never be a real token: this file is tracked, and a fixture here
# ships to anyone who clones the repo (dv-2f70). Shape only needs to satisfy
# _TELEGRAM_TOKEN in src/utils/log_redaction.py: \d{5,}:[A-Za-z0-9_-]{20,}
TOKEN = "1234567890:AAHfakefakefakefakefakefakefakefake"


def _record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_redacts_telegram_token_in_url() -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    out = redact(f'HTTP Request: POST {url} "HTTP/1.1 200 OK"')
    assert TOKEN not in out
    assert REDACTED in out
    assert "api.telegram.org/bot" in out  # the useful part survives


def test_redacts_url_encoded_token_in_file_download() -> None:
    encoded = TOKEN.replace(":", "%3A")
    line = f"HTTP Request: GET https://api.telegram.org/file/bot{encoded}/voice/f.oga"
    out = redact(line)
    assert encoded not in out and TOKEN.split(":")[1] not in out
    assert f"/file/bot{REDACTED}/voice" in out


def test_redacts_query_string_secrets() -> None:
    out = redact("GET https://x.dev/v1?api_key=sk-abc123&page=2")
    assert "sk-abc123" not in out
    assert "page=2" in out


def test_filter_rewrites_record_message() -> None:
    record = _record(f"HTTP Request: POST https://api.telegram.org/bot{TOKEN}/x")
    assert SecretRedactingFilter().filter(record) is True
    assert TOKEN not in record.getMessage()


def test_filter_handles_lazy_format_args() -> None:
    record = _record("HTTP Request: %s", (f"https://t.me/bot{TOKEN}/x",))
    SecretRedactingFilter().filter(record)
    assert TOKEN not in record.getMessage()


def test_filter_leaves_clean_messages_untouched() -> None:
    record = _record("nothing to see here")
    SecretRedactingFilter().filter(record)
    assert record.getMessage() == "nothing to see here"


def test_install_is_idempotent() -> None:
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        install_secret_redaction()
        install_secret_redaction()
        installed = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
        assert len(installed) == 1
    finally:
        root.removeHandler(handler)
