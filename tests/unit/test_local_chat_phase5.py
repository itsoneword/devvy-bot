"""Phase 5 contract: local chat uses the same durable turn pipeline as Telegram.

The local HTTP entry point is deliberately exercised with a fake turn service:
it makes the channel boundary observable without starting a model process.
The turn service itself is covered by the real SQLite window-cycle tests.
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus
from src.tui.client import PumpClient

TUI_AUTH = {"Authorization": "Bearer tui-secret"}


def _settings(channel="local"):
    settings = MagicMock()
    settings.channel = channel
    settings.local_tui_secret = "tui-secret"
    settings.webhook_api_secret = "internal-secret"
    settings.development_mode = True
    return settings


class RecordingTurns:
    """Fake model-facing service, leaving HTTP streaming and routing real."""

    def __init__(self):
        self.calls = []
        self.sessions = {}

    async def stream(self, *, user_id, profile, text):
        session = self.sessions.setdefault(profile, f"session-{profile}")
        self.calls.append((user_id, profile, text, session))
        yield {"type": "delta", "text": "first "}
        yield {"type": "delta", "text": "second"}
        yield {"type": "done", "session_id": session}


def _app(turns, channel="local"):
    return create_api_app(EventBus(), _settings(channel), chat_turn_service=turns)


def test_local_chat_streams_reply_from_selected_agent():
    turns = RecordingTurns()
    with TestClient(_app(turns)) as client:
        with client.stream(
            "POST",
            "/internal/chat",
            json={"profile": "devops", "text": "hello"},
            headers=TUI_AUTH,
        ) as response:
            assert response.status_code == 200
            parts = [line for line in response.iter_lines() if line]
    payload = "\n".join(parts)
    assert "first " in payload and "second" in payload
    assert "session-devops" in payload
    assert turns.calls[0][1:3] == ("devops", "hello")


def test_agent_tabs_keep_separate_sessions_when_switching_back():
    turns = RecordingTurns()
    with TestClient(_app(turns)) as client:
        for profile in ("devops", "chef", "devops"):
            response = client.post(
                "/internal/chat",
                json={"profile": profile, "text": "next"},
                headers=TUI_AUTH,
            )
            assert response.status_code == 200
    assert [call[3] for call in turns.calls] == [
        "session-devops",
        "session-chef",
        "session-devops",
    ]


def test_local_chat_stream_preserves_roll_and_next_turn_seed():
    """The channel must pass rollover and seed events through unchanged.

    The real threshold calculation is guarded by test_core_window_cycle; this
    checks that the local streaming adapter does not hide its result.
    """

    class RollingTurns(RecordingTurns):
        async def stream(self, *, user_id, profile, text):
            async for event in super().stream(
                user_id=user_id, profile=profile, text=text
            ):
                yield event
            if text == "threshold":
                yield {"type": "window_rolled", "window_id": 2}
            elif text == "after threshold":
                yield {"type": "seeded", "from_window": 1}

    with TestClient(_app(RollingTurns())) as client:
        crossing = client.post(
            "/internal/chat",
            json={"profile": "devops", "text": "threshold"},
            headers=TUI_AUTH,
        )
        seeded = client.post(
            "/internal/chat",
            json={"profile": "devops", "text": "after threshold"},
            headers=TUI_AUTH,
        )
    assert crossing.status_code == 200 and "window_rolled" in crossing.text
    assert seeded.status_code == 200 and "seeded" in seeded.text


class ChatApi:
    def __init__(self):
        self.calls = []

    def stream(self, path, json):
        self.calls.append((path, json))
        yield {"type": "delta", "text": "Hi"}
        yield {"type": "done", "session_id": "s1"}


def test_tui_chat_sends_text_to_the_active_agent_and_shows_stream():
    api = ChatApi()
    client = PumpClient(api=api)
    client.select_agent("devops")
    assert list(client.chat("  hello\nworld  ")) == [
        {"type": "delta", "text": "Hi"},
        {"type": "done", "session_id": "s1"},
    ]
    assert api.calls == [
        ("/internal/chat", {"profile": "devops", "text": "  hello\nworld  "})
    ]


def test_tui_chat_switching_tabs_keeps_each_agents_conversation():
    api = ChatApi()
    client = PumpClient(api=api)
    client.select_agent("devops")
    list(client.chat("one"))
    client.select_agent("chef")
    list(client.chat("two"))
    client.select_agent("devops")
    assert [body["profile"] for _, body in api.calls] == ["devops", "chef"]
    assert client.active_agent == "devops"
    assert [m["text"] for m in client.chat_history()] == ["one", "Hi"]


def test_local_chat_denied_tool_call_does_not_continue_turn():
    """An ask must surface through the local TUI and a denial must stop it."""

    class AskApi(ChatApi):
        denied = False

        def stream(self, path, json):
            self.calls.append((path, json))
            yield {"type": "ask", "text": "Delete files?", "data": "confirm:t:n"}
            if not self.denied:
                yield {"type": "tool_executed", "tool": "Bash"}
            yield {"type": "done", "denied": self.denied}

        def post(self, path, json):
            self.calls.append((path, json))
            self.denied = json == {"data": "confirm:t:n"}
            return {"matched": True}

    api = AskApi()
    client = PumpClient(api=api)
    client.select_agent("devops")
    stream = client.chat("delete files")
    ask = next(stream)
    assert ask["type"] == "ask"
    client.deny_chat_ask(ask)
    events = list(stream)
    assert ("/internal/callback", {"data": "confirm:t:n"}) in api.calls
    assert events[-1]["denied"] is True
    assert not any(e["type"] == "tool_executed" for e in events)


def test_telegram_chat_endpoint_is_not_mounted():
    """The local credential must not create a chat route on a Telegram bot."""
    with TestClient(create_api_app(EventBus(), _settings("telegram"))) as client:
        response = client.post(
            "/internal/chat",
            json={"profile": "devops", "text": "hi"},
            headers=TUI_AUTH,
        )
    assert response.status_code == 404


def test_local_chat_rejects_the_job_internal_bearer():
    """A boxed job must not be able to speak as the TUI owner."""
    turns = RecordingTurns()
    with TestClient(_app(turns)) as client:
        response = client.post(
            "/internal/chat",
            json={"profile": "devops", "text": "run this"},
            headers={"Authorization": "Bearer internal-secret"},
        )
    assert response.status_code in (401, 403)
    assert turns.calls == []


def test_empty_chat_message_does_not_start_a_turn():
    turns = RecordingTurns()
    with TestClient(_app(turns)) as client:
        response = client.post(
            "/internal/chat",
            json={"profile": "devops", "text": "   "},
            headers=TUI_AUTH,
        )
    assert response.status_code == 400 or response.status_code == 422
    assert turns.calls == []
