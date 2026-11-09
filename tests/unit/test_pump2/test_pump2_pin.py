"""dv-5c22: one pinned pump2 summary per board, edited in place; no buttons."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.pump2 import pin
from src.pump2.state import Board, add_comment, set_pump2, set_status

from .test_pump2 import card, git

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
ASK = (
    'analyze: questions for the owner (answer with `pump2 answer t:dv-0002 "x"`)\n'
    "1. Should we use A or B?\n   - (a) A\n   - (b) B\n"
)


class Notify:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    def __call__(self, argv: list[str]) -> None:
        if self.fail:
            raise RuntimeError("bot down")
        self.calls.append(list(argv))

    def pins(self) -> list[list[str]]:
        return [c for c in self.calls if "--pin-as" in c]

    def msgs(self) -> list[list[str]]:
        """Messages to the board agent's chat."""
        return [c for c in self.calls if "--profile" in c]

    def topic(self) -> list[str]:
        """Lines sent to the Pump topic (merges, questions, stops)."""
        return [c[2] for c in self.calls if pin.NEWS_SOURCE in c]


class Router:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, prompt: str, **kw: Any) -> str:
        self.calls.append({"prompt": prompt, **kw})
        return "Promote dv-0003 first, then wait."


@pytest.fixture
def b(tmp_path: Path) -> Board:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return Board(name="t", repo=r, profile="app")


def mk(b: Board, cid: str, title: str, stage: str, status: str = "in_progress") -> Path:
    p = card(b.repo, cid, status=status)
    text = p.read_text().replace("title: t\n", f"title: {title}\n")
    p.write_text(text)
    set_pump2(p, stage=stage)
    return p


def run(b: Board, n: Notify, r: Router, tmp: Path) -> None:
    pin.refresh(b, notify=n, complete=r, state_dir=tmp / "state", now=NOW)
    pin.send_all([b], notify=n, state_dir=tmp / "state")


def arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_render_sections_and_rows(b: Board, tmp_path: Path) -> None:
    mk(b, "dv-0001", "Add the shiny widget to dashboard now", "build")
    p2 = mk(b, "dv-0002", "Pick a database", "analyze", "waiting_user")
    add_comment(p2, ASK)
    p3 = mk(b, "dv-0003", "Broken thing", "stopped")
    add_comment(p3, "stopped: tests red twice")
    wt = b.repo / ".pump-worktrees/dv-0001/.pump"
    wt.mkdir(parents=True)
    (wt / "job-build.log").write_text("compiling stuff\nlast words here\n")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)
    text = n.pins()[0][2]
    for sec in (
        "In work",
        "Waiting for you",
        "On staging, not on prod",
        "On prod",
        "Recommendation",
    ):
        assert sec in text
    assert "dv-0001" in text and "build" in text and "last words here" in text
    assert "Add the shiny widget to dashboard" in text
    assert "now" not in text.split("dv-0001")[1].split("\n")[0].split()[-1:]
    assert "Should we use A or B?" in text and "- (b) B" in text
    assert "dv-0003" in text and "tests red twice" in text


def test_recommendation_called_only_on_change(b: Board, tmp_path: Path) -> None:
    p1 = mk(b, "dv-0001", "One", "build")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)
    run(b, n, r, tmp_path)
    assert len(r.calls) == 1 and r.calls[0]["purpose"] == "pump_summary"
    mk(b, "dv-0002", "Two", "tests")  # add
    run(b, n, r, tmp_path)
    assert len(r.calls) == 2
    set_pump2(p1, stage="review")  # stage change
    run(b, n, r, tmp_path)
    assert len(r.calls) == 3
    set_status(p1, "waiting_user")  # status change
    run(b, n, r, tmp_path)
    assert len(r.calls) == 4
    run(b, n, r, tmp_path)
    assert len(r.calls) == 4
    assert "Promote dv-0003" in n.pins()[-1][2]


def test_router_failure_keeps_previous_recommendation(b: Board, tmp_path: Path) -> None:
    mk(b, "dv-0001", "One", "build")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)

    async def boom(prompt: str, **kw: Any) -> str:
        raise RuntimeError("router down")

    mk(b, "dv-0002", "Two", "tests")
    pin.refresh(b, notify=n, complete=boom, state_dir=tmp_path / "state", now=NOW)
    assert "Promote dv-0003" in n.pins()[-1][2]


def test_question_and_stop_messages_once(b: Board, tmp_path: Path) -> None:
    p = mk(b, "dv-0002", "Pick a database", "analyze")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)
    assert n.msgs() == []
    add_comment(p, ASK)
    set_status(p, "waiting_user")
    run(b, n, r, tmp_path)
    assert len(n.msgs()) == 1
    m = n.msgs()[0]
    assert arg(m, "--profile") == "app" and "--deliver" in m
    assert arg(m, "--ref") == "pump:t:dv-0002"
    run(b, n, r, tmp_path)
    assert len(n.msgs()) == 1
    q = mk(b, "dv-0004", "Other", "build")
    run(b, n, r, tmp_path)
    set_pump2(q, stage="stopped")
    run(b, n, r, tmp_path)
    assert [arg(x, "--ref") for x in n.msgs()] == [
        "pump:t:dv-0002",
        "pump:t:dv-0004",
    ]
    run(b, n, r, tmp_path)
    assert len(n.msgs()) == 2


def test_failed_send_retries_next_tick(b: Board, tmp_path: Path) -> None:
    p = mk(b, "dv-0002", "Pick", "analyze", "waiting_user")
    add_comment(p, ASK)
    n, r = Notify(), Router()
    n.fail = True
    run(b, n, r, tmp_path)  # must not raise
    n.fail = False
    run(b, n, r, tmp_path)
    assert len(n.msgs()) == 1


def test_pin_edit_flags_and_no_buttons(b: Board, tmp_path: Path) -> None:
    mk(b, "dv-0001", "One", "build")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)
    c = n.pins()[0]
    assert arg(c, "--pin-as") == "pump2"
    # the pin goes to the Pump reports topic, not into the agent's chat
    assert "--deliver" not in c and arg(c, "--source") == "pump2:status"
    assert all("--buttons-json" not in x for x in n.calls)
    for f in (ROOT / "src/pump2").glob("*.py"):
        if f.name == "feedback.py":
            # dv-a42c: the owner asked for score buttons on the feature
            # survey; the pin and pump questions stay without tap targets.
            continue
        assert "buttons" not in f.read_text(), f.name


def _deploy_repo(tmp_path: Path) -> tuple[Board, Path, list[str]]:
    r = tmp_path / "g"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    shas = []
    for i in range(3):
        (r / "f").write_text(str(i))
        git(r, "add", "-A")
        git(r, "commit", "-qm", f"c{i}")
        shas.append(git(r, "rev-parse", "HEAD"))
    log = tmp_path / "deploy.log"
    log.write_text(
        f"2026-11-05T10:00:00Z 000 -> {shas[0]} build=1 ok\n"
        f"2026-11-05T11:00:00Z {shas[0]} -> {shas[2]} staging build=2 ok\n"
    )
    return Board(name="g", repo=r, branch="master"), log, shas


def test_gymbro_prod_and_staging(tmp_path: Path) -> None:
    b, log, shas = _deploy_repo(tmp_path)
    # shas[1] belongs to a card merged after prod; shas[0] is on prod already
    for cid, sha in (("dv-0010", shas[0]), ("dv-0011", shas[1])):
        p = card(b.repo, cid, status="done")
        set_pump2(p, stage="done", merged=sha)
    view = pin.deploy_view(b, deploy_log=log)
    assert view["prod_sha"].startswith(shas[0][:7])
    assert [cid for cid, _ in view["staging"]] == ["dv-0011"]


def test_devvy_prod_from_activation_time(tmp_path: Path) -> None:
    b, _log, shas = _deploy_repo(tmp_path)
    p = card(b.repo, "dv-0011", status="done")
    set_pump2(p, stage="done", merged=shas[2])
    when = datetime(2026, 1, 1, tzinfo=UTC)  # before every commit
    view = pin.deploy_view(b, activated_at=when)
    assert [cid for cid, _ in view["staging"]] == ["dv-0011"]
    none = pin.deploy_view(b)
    assert none["prod_sha"] in ("", None, "unknown") and none["staging"] == []
    assert "unknown" in pin.render_pin(b, [], none, "", now=NOW)


def test_merge_stores_merged_sha(tmp_path: Path) -> None:
    src = (ROOT / "src/pump2/merge.py").read_text()
    assert "merged=" in src


def test_dry_run_prints_without_sending(
    b: Board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from src.pump2 import cli

    mk(b, "dv-0001", "One", "build")
    bf = tmp_path / "boards.json"
    bf.write_text(
        json.dumps({"boards": [{"name": "t", "repo": str(b.repo), "profile": "app"}]})
    )

    def no(*a: Any, **k: Any) -> Any:
        raise AssertionError("sent")

    monkeypatch.setattr(pin, "send", no, raising=False)
    monkeypatch.setattr(subprocess, "run", no)
    assert cli.main(["--boards", str(bf), "pin", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "In work" in out and "dv-0001" in out


def test_tick_refreshes_pin_per_board_and_survives_errors(
    b: Board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_pump2 import Fake

    seen: list[str] = []
    monkeypatch.setattr(pin, "refresh", lambda bd, **k: seen.append(bd.name))
    Fake().tick(b, tmp_path)
    assert seen == ["t"]

    def boom(bd: Board, **k: Any) -> None:
        raise RuntimeError("bot down")

    monkeypatch.setattr(pin, "refresh", boom)
    Fake().tick(b, tmp_path)  # no exception


def test_summary_route_and_docs() -> None:
    from src.model_router.routes import DEFAULT_CALL_ROUTES

    assert "pump_summary" in DEFAULT_CALL_ROUTES
    assert "pump_summary" in (ROOT / "config/models.example.yaml").read_text()
    cap = (ROOT / "data/shared-capabilities.md").read_text()
    assert "pump2" in cap and "deploy command your persona names" in cap
    assert "restart_bot" in cap
    assert "pump_answer" in cap


def test_pin_source_routes_to_pump_topic() -> None:
    from src.notifications.routing import source_matches
    from src.pump2.pin import SOURCE
    from src.utils.constants import DEFAULT_REPORTS_ROUTES

    keys = [k for k in DEFAULT_REPORTS_ROUTES if source_matches(SOURCE, k)]
    assert keys and DEFAULT_REPORTS_ROUTES[max(keys, key=len)] == "Pump"


def test_one_pin_for_all_boards(b: Board, tmp_path: Path) -> None:
    # 06.11: per-board pins in one Pump topic cleared each other; now one pin
    mk(b, "dv-0001", "Busy card", "build")
    r2 = tmp_path / "repo2"
    r2.mkdir()
    git(r2, "init", "-q", "-b", "main")
    (r2 / "a.txt").write_text("a\n")
    git(r2, "add", "-A")
    git(r2, "commit", "-qm", "init")
    quiet = Board(name="q", repo=r2, profile="devops")
    n, r = Notify(), Router()
    for board in (b, quiet):
        pin.refresh(board, notify=n, complete=r, state_dir=tmp_path / "s", now=NOW)
    assert n.pins() == []  # refresh alone never pins
    pin.send_all([b, quiet], notify=n, state_dir=tmp_path / "s")
    (c,) = n.pins()
    assert arg(c, "--pin-as") == "pump2" and "--deliver" not in c
    text = c[2]
    assert "Busy card" in text and "pump2 · q: nothing in work or waiting" in text


def test_topic_gets_merges_questions_and_stops_once(b: Board, tmp_path: Path) -> None:
    old = mk(b, "dv-0001", "Merged before", "done")
    set_pump2(old, since="2026-01-01T00:00:00Z")
    n, r = Notify(), Router()
    run(b, n, r, tmp_path)
    assert n.topic() == []  # no backlog of cards merged before the first tick
    p = mk(b, "dv-0002", "Pick a database", "analyze")
    add_comment(p, ASK)
    set_status(p, "waiting_user")
    q = mk(b, "dv-0003", "Fast merge", "merge")
    add_comment(q, "merge: merged 102ff7dea487f59058b50408b20673b6fb67c1fd into main")
    set_pump2(q, stage="done", since="2099-01-01T00:00:00Z")
    run(b, n, r, tmp_path)
    run(b, n, r, tmp_path)
    lines = n.topic()
    assert len(lines) == 2
    assert any(x.startswith("❓") and "dv-0002" in x for x in lines)
    assert "✅ t:dv-0003 Fast merge merged 102ff7de" in lines
    assert len(n.msgs()) == 1  # the question still goes to the board's chat


def test_news_source_routes_to_pump_topic() -> None:
    from src.notifications.routing import source_matches
    from src.utils.constants import DEFAULT_REPORTS_ROUTES

    keys = [k for k in DEFAULT_REPORTS_ROUTES if source_matches(pin.NEWS_SOURCE, k)]
    assert keys and DEFAULT_REPORTS_ROUTES[max(keys, key=len)] == "Pump"
