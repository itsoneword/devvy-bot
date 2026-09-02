"""An oversized stream-json message must surface as a parsing error.

Live incident (coach, 2026-08-04): after charts moved to matplotlib in-sandbox,
the model rendered a PNG and then `Read` it back to check it. The CLI returns
the image base64-encoded in a single stream-json message; at 611 KB of PNG that
message passed the SDK's 1 MiB stdout limit and the turn died with
"Unexpected error: Failed to decode JSON: JSON message exceeded maximum buffer
size of 1048576 bytes".

Two bugs, one symptom:
  * `except CLIJSONDecodeError` is unreachable — the SDK's message reader
    catches the typed exception and re-emits it as `{"type": "error", ...}`,
    which arrives here as a bare `Exception` carrying only the text.
  * `max_buffer_size` was never set, so the ceiling stayed at the 1 MiB default.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.claude.exceptions import ClaudeParsingError
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings

from .test_sdk_integration import (  # noqa: F401  (autouse fixture, needs importing)
    _make_result_message,
    _mock_client_factory,
    _patch_parse_message,
)

_BUFFER_ERROR = (
    "Failed to decode JSON: JSON message exceeded maximum buffer size "
    "of 1048576 bytes..."
)


@pytest.fixture
def config(tmp_path):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=2,
        enable_mcp=False,
    )


@pytest.fixture
def sdk_manager(config):
    return ClaudeSDKManager(config)


def _client_raising(exc):
    """A mock client whose message stream blows up mid-iteration."""

    def factory(options):
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def receive_raw_messages():
            raise exc
            yield  # pragma: no cover - makes this an async generator

        query_mock = AsyncMock()
        query_mock.receive_messages = receive_raw_messages
        client._query = query_mock
        return client

    return factory


class TestOversizedStreamMessage:
    async def test_bare_exception_from_reader_maps_to_parsing_error(self, sdk_manager):
        """The SDK strips the type; we must still classify it by its text."""
        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_client_raising(Exception(_BUFFER_ERROR)),
        ):
            with pytest.raises(ClaudeParsingError) as excinfo:
                await sdk_manager.execute_command(
                    prompt="render a chart",
                    working_directory=Path("/test"),
                )

        assert "buffer size" in str(excinfo.value)

    async def test_buffer_ceiling_is_raised_above_the_sdk_default(
        self, sdk_manager, config
    ):
        """A one-megabyte cap is smaller than a single chart PNG."""
        captured = []
        mock_factory = _mock_client_factory(
            _make_result_message(result="done"), capture_options=captured
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="hi",
                working_directory=Path("/test"),
            )

        assert captured
        assert captured[0].max_buffer_size == config.claude_max_buffer_size
        assert captured[0].max_buffer_size > 1024 * 1024
