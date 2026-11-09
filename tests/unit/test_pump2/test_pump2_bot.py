"""Pump v2 CLI subcommands the bot calls (dv-d474): start, pending, status --json."""

import json
from pathlib import Path

import pytest

from src.pump2.cli import main as cli_main
from src.pump2.state import add_comment, find_card, read_card, set_pump2, set_status

from .test_pump2 import TASKS, board, card
from .test_pump2 import repo as _repo  # noqa: F401

repo = _repo


@pytest.fixture
def bf(repo: Path) -> Path:
    p = repo / "boards.json"
    p.write_text(f'{{"boards":[{{"name":"t","repo":"{repo}"}}]}}')
    return p


def test_start_sets_stage_and_status(repo: Path, bf: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    card(repo, "dv-0002", status="todo")
    assert cli_main(["--boards", str(bf), "start", "t:dv-0002"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out.splitlines()) == 1 and "dv-0002" in out
    c = read_card(repo / TASKS / "dv-0002.md")
    assert c.pump2["stage"] == "analyze"
    assert "pump" not in c.fm
    assert c.fm["status"] == "in_progress"


def test_start_refuses_a_card_already_in_pump2(repo: Path, bf: Path) -> None:
    p = repo / TASKS / "dv-0001.md"
    set_pump2(p, stage="build")
    before = p.read_text()
    assert cli_main(["--boards", str(bf), "start", "t:dv-0001"]) == 2
    assert p.read_text() == before


def _ask(repo: Path, cid: str) -> None:
    p = repo / TASKS / f"{cid}.md"
    set_pump2(p, stage="analyze", round=1)
    set_status(p, "waiting_user")
    add_comment(
        p,
        "analyze: questions for the owner (answer with `pump2 answer t:x`)\n"
        "1. Delete the old table?\n2. Keep the cache?\n\nplan ready",
    )


def test_pending_json_records(repo: Path, bf: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _ask(repo, "dv-0001")
    card(repo, "dv-0003", status="waiting_user")  # no pump2.stage: not ours
    assert cli_main(["--boards", str(bf), "pending", "--json"]) == 0
    recs = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in recs] == ["t:dv-0001"]
    r = recs[0]
    assert r["board"] == "t" and r["title"] == "t"
    assert r["pump"] == "asked" and r["kind"] == "question"
    assert "Delete the old table?" in json.dumps(r["questions"])
    assert "questions for the owner" in r["block"]


def test_pending_plain_and_empty(repo: Path, bf: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli_main(["--boards", str(bf), "pending"]) == 0
    assert "no task is waiting for an answer" in capsys.readouterr().out
    _ask(repo, "dv-0001")
    assert cli_main(["--boards", str(bf), "pending"]) == 0
    assert "=== t:dv-0001" in capsys.readouterr().out


def test_status_json_rows_and_groups(repo: Path, bf: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _ask(repo, "dv-0001")
    p = card(repo, "dv-0004", status="in_progress")
    set_pump2(p, stage="build")
    p = card(repo, "dv-0005", status="in_progress")
    set_pump2(p, stage="stopped")
    assert cli_main(["--boards", str(bf), "status", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    keys = {"id", "title", "board", "pump", "status", "group", "icon", "why"}
    keys |= {"act", "hours", "first"}
    for r in rows.values():
        assert keys <= set(r)
    assert rows["t:dv-0001"]["group"] == "needs"
    assert rows["t:dv-0005"]["group"] == "stopped"
    assert rows["t:dv-0004"]["group"] in ("running", "queued")


def test_answer_after_double_dash_is_verbatim(repo: Path, bf: Path) -> None:
    _ask(repo, "dv-0001")
    text = "-1 no, --force"
    assert cli_main(["--boards", str(bf), "answer", "t:dv-0001", "--", text]) == 0
    c = find_card(board(repo, "true"), "dv-0001")
    assert c.fm["status"] == "in_progress"
    assert "### @owner" in c.body and text in c.body
