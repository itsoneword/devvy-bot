"""dv-5c22 review: the pin stays under Telegram's limit; the home board's
prod sha comes from when main moved (reflog), not from commit dates."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from src.pump2 import pin
from src.pump2.state import Board, add_comment, list_cards, set_pump2

from .test_pump2 import card
from .test_pump2_pin import NOW, b, mk  # noqa: F401  (b is a fixture)


def _git(repo: Path, *a: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return r.stdout.strip()


def test_pin_with_long_questions_fits_telegram(b: Board) -> None:  # noqa: F811
    long_ask = "analyze: questions for the owner\n" + "".join(
        f"{n}. " + "Which way should this go, and why exactly? " * 4 + "\n"
        "   - (a) " + "the first option, explained at length " * 3 + "\n"
        "   - (b) " + "the second option, explained at length " * 3 + "\n"
        for n in range(1, 5)
    )
    assert len(long_ask) > 900
    ids = [f"dv-00{n:02d}" for n in range(1, 6)]
    for cid in ids:
        p = mk(b, cid, f"Long question card {cid}", "analyze", "waiting_user")
        add_comment(p, long_ask)
    cards = list_cards(b)[0]
    view = {
        "prod_sha": "abc1234",
        "prod_time": "x",
        "staging": [(f"dv-01{n:02d}", "deadbee") for n in range(60)],
    }
    text = pin.render_pin(b, cards, view, "Promote first.", now=NOW)
    assert len(text) <= 4096
    for cid in ids:
        assert cid in text
    assert "more" in text
    # the one-shot message still carries the full question
    news = pin.news_text(b, next(c for c in cards if c.id == ids[0]))
    assert "the second option" in news and len(news) > 900


def test_home_prod_uses_reflog_not_commit_dates(tmp_path: Path) -> None:
    r = tmp_path / "home"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("0")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base", when="2026-11-01T10:00:00Z")
    base = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-qb", "feat")
    (r / "f").write_text("1")
    _git(r, "commit", "-qam", "card", when="2026-11-01T11:00:00Z")
    feat = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    # fast-forwarded after the bot started; the commit is dated before it
    _git(r, "merge", "-q", "--ff-only", "feat", when="2026-11-01T13:00:00Z")
    home = Board(name="devvy", repo=r, branch="main")
    p = card(r, "dv-0021", status="done")
    set_pump2(p, stage="done", merged=feat)
    started = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    view = pin.deploy_view(home, activated_at=started)
    assert view["prod_sha"] == base[:7]
    assert [cid for cid, _ in view["staging"]] == ["dv-0021"]
    later = pin.deploy_view(home, activated_at=datetime(2026, 11, 1, 14, tzinfo=UTC))
    assert later["prod_sha"] == feat[:7] and later["staging"] == []
