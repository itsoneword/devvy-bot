"""dv-f707: analysis questions go through resolve (the board's agent) first."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.pump2 import pin, render, runner  # noqa: F401
from src.pump2.state import Board, find_card, list_cards, set_pump2

from .test_pump2 import PLAN, TASKS, Fake, card, git, plan, start  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
Q1 = "Q: Show the sketch in the live app? | default: yes | matters: look | found: f"
Q2 = "Q: Cancel signed-up sessions? | default: no | matters: people | found: g"
QS = f"{Q1}\n{Q2}"
CID = "dv-0001"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    card(r, CID)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def mkboard(repo: Path) -> Board:
    return Board(name="t", repo=repo, test="true", profile="gymbro")


def to_question_stop(repo: Path, tmp: Path, qs: str = QS, resolves: int = 0):  # type: ignore[no-untyped-def]
    b, f = mkboard(repo), Fake()
    start(repo)
    f.tick(b, tmp)
    if resolves:
        set_pump2(repo / TASKS / f"{CID}.md", resolves=resolves)
    plan(repo, "code", qs)
    f.tick(b, tmp)
    return b, f


def resolve_with(b: Board, f: Fake, repo: Path, tmp: Path, text: str) -> None:
    f.tick(b, tmp)  # launches resolve
    assert f.launched[-1][1] == "resolve"
    wt = repo / ".pump-worktrees" / CID / ".pump"
    (wt / "resolve.md").write_text(text)
    f.tick(b, tmp)  # finish


def report(verdict: str, extra: str) -> str:
    return f"## Verdict\n{verdict}\n\n## Reasoning\nreasons\n\n{extra}\n"


def test_C1_questions_go_to_resolve_not_owner(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    c = find_card(b, CID)
    assert c.pump2["stage"] == "resolve"
    assert c.pump2.get("resolve_from") == "analyze"
    assert c.fm["status"] == "in_progress"
    assert "analyze: questions for the owner" not in c.body
    stop = (repo / ".pump-worktrees" / CID / ".pump" / "stop.md").read_text()
    assert "1. Q: Show the sketch" in stop and "2. Q: Cancel" in stop


def test_C2_agent_answers_reruns_analyze(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    ans = "## Answers\n1. take the default; flip back with one flag\n2. keep sessions; flip in settings"
    resolve_with(b, f, repo, tmp_path, report("rerun analyze", ans))
    c = find_card(b, CID)
    assert c.pump2["stage"] == "analyze" and not c.pump2.get("rethink")
    assert c.fm["status"] == "in_progress"
    assert "/gymbro — " in c.body
    assert "take the default; flip back with one flag" in c.body
    assert "analyze: questions for the owner" not in c.body
    f.tick(b, tmp_path)
    p = f.launched[-1][3]
    assert f.launched[-1][1] == "analyze"
    assert "take the default; flip back with one flag" in p
    assert "apply" in p.lower()


def test_C2_missing_answer_number_rejected(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    resolve_with(
        b, f, repo, tmp_path, report("rerun analyze", "## Answers\n1. take default")
    )
    c = find_card(b, CID)
    assert c.pump2["stage"] != "analyze"
    assert c.pump2["stage"] in ("stopped", "resolve")
    assert "/gymbro — " not in c.body


def test_C3_owner_mix_passes_questions_unchanged(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    text = report(
        "owner",
        "## Answers\n1. take the default; flip back with one flag\n\n"
        "## Owner reason\nproduct choice",
    )
    resolve_with(b, f, repo, tmp_path, text)
    c = find_card(b, CID)
    assert c.fm["status"] == "waiting_user" and c.pump2["stage"] == "analyze"
    assert "analyze: questions for the owner" in c.body
    assert f"2. {Q2}" in c.body
    ask = c.body.split("analyze: questions for the owner")[-1]
    assert Q1 not in ask
    assert "take the default; flip back with one flag" in c.body  # recorded
    assert render.questions_of(c)  # pending sees it
    bf = repo / "boards.json"
    bf.write_text(f'{{"boards":[{{"name":"t","repo":"{repo}"}}]}}')
    from src.pump2.cli import main as cli_main

    assert cli_main(["--boards", str(bf), "answer", f"t:{CID}", "no cancel"]) == 0
    assert find_card(b, CID).fm["status"] == "in_progress"


def test_C3_owner_needs_valid_reason(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    resolve_with(
        b,
        f,
        repo,
        tmp_path,
        report("owner", "## Answers\n1. d\n\n## Owner reason\nbecause I feel like it"),
    )
    assert find_card(b, CID).fm["status"] != "waiting_user"


def test_C4_second_round_is_asked_twice_stop(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    resolve_with(
        b,
        f,
        repo,
        tmp_path,
        report("rerun analyze", "## Answers\n1. a\n2. b"),
    )
    f.tick(b, tmp_path)  # analyze relaunch
    plan(repo, "code", Q1)
    f.tick(b, tmp_path)
    c = find_card(b, CID)
    assert c.pump2["stage"] == "resolve" and "asked twice" in c.body


def test_C4_question_stop_at_resolve_max_still_triaged(
    repo: Path, tmp_path: Path
) -> None:
    from src.pump2.state import RESOLVE_MAX

    b, f = to_question_stop(repo, tmp_path, resolves=RESOLVE_MAX)
    c = find_card(b, CID)
    assert c.pump2["stage"] == "resolve"
    assert int(c.pump2["resolves"]) == RESOLVE_MAX


def test_C4_non_question_stop_at_resolve_max_goes_to_owner(
    repo: Path, tmp_path: Path
) -> None:
    from src.pump2.state import RESOLVE_MAX

    b, f = mkboard(repo), Fake()
    start(repo)
    f.tick(b, tmp_path)
    set_pump2(repo / TASKS / f"{CID}.md", resolves=RESOLVE_MAX)
    plan(repo, touches="")
    f.tick(b, tmp_path)
    f.tick(b, tmp_path)
    plan(repo, touches="")
    f.tick(b, tmp_path)
    assert find_card(b, CID).pump2["stage"] == "stopped"


_REPLAY = ROOT / "scripts" / "pump2_question_replay.py"


@pytest.mark.skipif(not _REPLAY.exists(), reason="private replay script not shipped")
def test_C5_replay_script_exists() -> None:
    s = _REPLAY.read_text()
    for needle in ("gb-c86d", "gb-cb22", "dv-040f", "complete_text", "pump_resolve"):
        assert needle in s


def test_C6_prompts_define_prod_as_server_action() -> None:
    import re

    from src.pump2.stages import analyze, resolve

    src = (ROOT / "src/pump2/stages/analyze.py").read_text()
    rs = (ROOT / "src/pump2/stages/resolve.py").read_text()
    for t in (src, rs):
        assert re.search(r"prod (server|data)", t)
    assert analyze and resolve


def test_C7_pin_shows_decided_for_you(repo: Path, tmp_path: Path) -> None:
    b, f = to_question_stop(repo, tmp_path)
    resolve_with(
        b,
        f,
        repo,
        tmp_path,
        report("rerun analyze", "## Answers\n1. took default zebra\n2. kept sessions"),
    )
    cards = list_cards(b)[0]
    view = {"prod_sha": "abc1234", "prod_time": "x", "staging": []}
    now = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    text = pin.render_pin(b, cards, view, "go", now=now)
    assert "Decided for you" in text and "zebra" in text and CID in text
    assert len(text) <= pin.PIN_MAX
    c = find_card(b, CID)
    assert pin.news_text(b, c) is not None
