from pathlib import Path

from src.bot.orchestration.formatting import format_sessions_summary
from src.bot.orchestration.windowing import apply_window_tokens
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.storage.window_repository import WindowState


def test_summary_lines_for_profiles():
    reg = ProfileRegistry(
        [
            ProfileDefinition(
                "devops",
                "DevOps",
                Path("/tmp/dev"),
                Path("/tmp/dev/m"),
                Path("/tmp/dev/p.md"),
                default=True,
            ),
            ProfileDefinition(
                "channel",
                "Channel",
                Path("/tmp/chan"),
                Path("/tmp/chan/m"),
                Path("/tmp/chan/p.md"),
            ),
        ]
    )

    # last-activity lookup keyed by working dir
    last = {
        Path("/tmp/dev"): {
            "cumulative_tokens": 1200,
            "updated_at": "2026-05-30T10:00:00+00:00",
        },
        Path("/tmp/chan"): None,
    }
    text = format_sessions_summary(reg, last, active="devops")
    assert "devops" in text and "channel" in text
    assert "1200" in text
    assert "no session" in text.lower()


def _registry() -> ProfileRegistry:
    return ProfileRegistry(
        [
            ProfileDefinition(
                "devops",
                "DevOps",
                Path("/tmp/dev"),
                Path("/tmp/dev/m"),
                Path("/tmp/dev/p.md"),
                default=True,
            ),
            ProfileDefinition(
                "channel",
                "Channel",
                Path("/tmp/chan"),
                Path("/tmp/chan/m"),
                Path("/tmp/chan/p.md"),
            ),
        ]
    )


def test_summary_emits_html_and_escapes_dynamic_values() -> None:
    reg = _registry()
    last = {
        Path("/tmp/dev"): {
            "cumulative_tokens": 42,
            "updated_at": "<2026-05-30 & later>",
            "backend": "claude",
        },
        Path("/tmp/chan"): None,
    }
    text = format_sessions_summary(reg, last, active="devops")
    assert text.startswith("<b>")
    assert "<2026" not in text  # dynamic values are HTML-escaped
    assert "&lt;2026-05-30 &amp; later&gt;" in text


def test_apply_window_tokens_overlays_authoritative_counts() -> None:
    reg = _registry()
    # Session records report 0 tokens for Claude sessions (the /sessions bug).
    last = {
        Path("/tmp/dev"): {"cumulative_tokens": 0, "updated_at": "t1"},
        Path("/tmp/chan"): {"cumulative_tokens": 0, "updated_at": "t2"},
    }
    windows = [
        WindowState(1, "/tmp/dev", 1, "claude", 9489, 0, 0),
    ]
    apply_window_tokens(reg, last, windows)
    assert last[Path("/tmp/dev")]["cumulative_tokens"] == 9489
    # No window row -> existing value untouched
    assert last[Path("/tmp/chan")]["cumulative_tokens"] == 0


def test_apply_window_tokens_skips_profiles_without_session_info() -> None:
    reg = _registry()
    last: dict = {Path("/tmp/dev"): None, Path("/tmp/chan"): None}
    windows = [WindowState(1, "/tmp/dev", 1, "claude", 500, 0, 0)]
    apply_window_tokens(reg, last, windows)
    assert last[Path("/tmp/dev")] is None
