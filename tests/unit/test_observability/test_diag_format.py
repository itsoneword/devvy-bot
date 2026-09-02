from src.bot.orchestration.formatting import format_diag


def test_format_diag_counts_events():
    events = [
        {"event": "resume", "detail": {}},
        {"event": "resume", "detail": {}},
        {"event": "fresh", "detail": {}},
        {"event": "memory_loaded", "detail": {"chars": 4000}},
        {"event": "memory_loaded", "detail": {"chars": 4400}},
        {"event": "refresh", "detail": {}},
    ]
    out = format_diag("devops", events)
    assert "devops" in out
    assert "resume" in out and "2" in out
    assert "fresh" in out and "1" in out
    assert "4200" in out or "4200.0" in out  # avg memory chars
    assert "refresh" in out


def test_format_diag_emits_html_not_markdown():
    out = format_diag("devops", [{"event": "resume", "detail": {}}])
    assert out.startswith("<b>")
    assert "*" not in out  # no raw Markdown entities left for Telegram to parse


def test_format_diag_escapes_profile_slug():
    # A slug with HTML/Markdown metacharacters must not break parse_mode="HTML"
    out = format_diag("dev_ops<x>&", [])
    assert "<x>" not in out
    assert "&lt;x&gt;&amp;" in out
    assert "no events recorded yet" in out
