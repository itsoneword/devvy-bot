"""Installer-side owner capture (dv-5c83).

The security property under test is the drain: "the owner is whoever messages
next" is only safe if everything Telegram was already holding is thrown away
first. Telegram keeps undelivered updates ~24h, so without the drain the very
first poll can hand ownership to a stranger's /start from yesterday.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capture_owner",
    Path(__file__).resolve().parents[2] / "scripts" / "capture_owner.py",
)
assert _SPEC and _SPEC.loader
capture_owner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(capture_owner)


class FakeApi:
    """Scripted getUpdates batches; records the offsets it was called with."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.offsets = []

    def __call__(self, token, method, http_timeout=30, **params):
        assert method == "getUpdates"
        self.offsets.append(params.get("offset"))
        return self.batches.pop(0) if self.batches else []


def _msg(update_id, user_id, *, is_bot=False, username=None):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id, "is_bot": is_bot, "username": username},
            "text": "/start",
        },
    }


# --- drain --------------------------------------------------------------


def test_drain_discards_backlog_and_reports_the_count(monkeypatch):
    api = FakeApi([[_msg(10, 111), _msg(11, 222)], [_msg(12, 333)], []])
    monkeypatch.setattr(capture_owner, "call", api)

    assert capture_owner.drain_updates("t") == 3


def test_drain_advances_offset_past_each_batch(monkeypatch):
    """Without offset=last+1 the same backlog comes back forever."""
    api = FakeApi([[_msg(10, 111), _msg(11, 222)], []])
    monkeypatch.setattr(capture_owner, "call", api)

    capture_owner.drain_updates("t")

    assert api.offsets == [None, 12]


def test_drain_on_a_clean_bot_reports_zero(monkeypatch):
    monkeypatch.setattr(capture_owner, "call", FakeApi([[]]))
    assert capture_owner.drain_updates("t") == 0


# --- wait ---------------------------------------------------------------


def test_wait_returns_the_first_human_sender(monkeypatch):
    api = FakeApi([[], [_msg(20, 111111111, username="me")]])
    monkeypatch.setattr(capture_owner, "call", api)

    sender, update_id = capture_owner.wait_for_sender(
        "t", 60, now=iter([0, 1, 2]).__next__
    )

    assert sender["id"] == 111111111
    assert update_id == 20


def test_wait_skips_bot_senders(monkeypatch):
    api = FakeApi([[_msg(20, 999, is_bot=True)], [_msg(21, 111111111)]])
    monkeypatch.setattr(capture_owner, "call", api)

    sender, _ = capture_owner.wait_for_sender("t", 60, now=iter([0, 1, 2, 3]).__next__)

    assert sender["id"] == 111111111


def test_wait_times_out_rather_than_polling_forever(monkeypatch):
    monkeypatch.setattr(capture_owner, "call", FakeApi([[], [], []]))

    with pytest.raises(TimeoutError, match="--owner-id"):
        capture_owner.wait_for_sender("t", 10, now=iter([0, 5, 20]).__next__)


def test_callback_query_counts_as_a_sender(monkeypatch):
    update = {"update_id": 30, "callback_query": {"from": {"id": 7, "is_bot": False}}}
    monkeypatch.setattr(capture_owner, "call", FakeApi([[update]]))

    sender, _ = capture_owner.wait_for_sender("t", 60, now=iter([0, 1]).__next__)

    assert sender["id"] == 7


# --- .env writing -------------------------------------------------------


def test_upsert_replaces_in_place_and_keeps_everything_else():
    body = "A=1\nALLOWED_USERS=old\nB=2\n"
    out = capture_owner.upsert_env(body, "ALLOWED_USERS", "42")
    assert out == "A=1\nALLOWED_USERS=42\nB=2\n"


def test_upsert_appends_when_key_is_absent():
    out = capture_owner.upsert_env("A=1\n", "ALLOWED_USERS", "42")
    assert out.endswith("ALLOWED_USERS=42\n")
    assert "A=1" in out


def test_upsert_replaces_the_last_assignment():
    """dotenv honours the last one; editing the first would change nothing."""
    body = "ALLOWED_USERS=first\nX=1\nALLOWED_USERS=second\n"
    out = capture_owner.upsert_env(body, "ALLOWED_USERS", "42")
    assert out == "ALLOWED_USERS=first\nX=1\nALLOWED_USERS=42\n"


def test_upsert_leaves_a_commented_key_as_documentation():
    body = "# ALLOWED_USERS=123456789\n"
    out = capture_owner.upsert_env(body, "ALLOWED_USERS", "42")
    assert "# ALLOWED_USERS=123456789" in out
    assert "\nALLOWED_USERS=42\n" in out


def test_write_env_backs_up_and_chmods(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")

    backup = capture_owner.write_env(env, {"ALLOWED_USERS": "42"})

    assert backup != env
    assert backup.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=old\n"
    assert "ALLOWED_USERS=42" in env.read_text(encoding="utf-8")
    assert env.stat().st_mode & 0o777 == 0o600


def test_write_env_creates_a_fresh_file(tmp_path):
    env = tmp_path / ".env"
    backup = capture_owner.write_env(env, {"ALLOWED_USERS": "42"})
    assert backup == env
    assert env.read_text(encoding="utf-8") == "ALLOWED_USERS=42\n"


# --- identity display ---------------------------------------------------


def test_describe_includes_the_id_so_confirmation_is_checkable():
    out = capture_owner.describe(
        {"id": 111111111, "first_name": "A", "last_name": "B", "username": "ab"}
    )
    assert "111111111" in out and "@ab" in out and "A B" in out


def test_describe_survives_a_sender_with_no_name():
    assert capture_owner.describe({"id": 5}) == "unknown (id 5)"


# --- --validate-only (used by install.sh step 2) ------------------------


def test_validate_only_prints_just_the_username_on_stdout(monkeypatch, capsys):
    """install.sh does BOT_USERNAME=$(... --validate-only), so anything else on
    stdout ends up in TELEGRAM_BOT_USERNAME and the bot answers to nothing."""
    monkeypatch.setattr(
        capture_owner,
        "get_me",
        lambda token: {"username": "mybot", "first_name": "My Bot"},
    )

    rc = capture_owner.main(["--token", "123:abc", "--validate-only"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "mybot\n"
    assert "@mybot" in captured.err


def test_validate_only_writes_nothing(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(
        capture_owner, "get_me", lambda token: {"username": "mybot", "first_name": ""}
    )

    rc = capture_owner.main(
        ["--token", "123:abc", "--env-file", str(env), "--validate-only"]
    )

    assert rc == 0
    assert not env.exists()


def test_validate_only_fails_fast_on_a_bad_token(monkeypatch, capsys):
    def boom(token):
        raise capture_owner.TelegramError("getMe failed: HTTP 401 Unauthorized")

    monkeypatch.setattr(capture_owner, "get_me", boom)

    rc = capture_owner.main(["--token", "nope", "--validate-only"])

    assert rc == 2
    assert capsys.readouterr().out == ""
