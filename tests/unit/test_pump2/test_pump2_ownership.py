"""dv-33c8: install / profile ownership on the board: authors set by code,
the pump takes only cards that are ours, 'Theirs' is shown apart."""

import json
import re
from pathlib import Path

import pytest

from src.pump2 import runner
from src.pump2.cli import main as cli_main
from src.pump2.state import list_cards, read_card

from .test_pump2 import TASKS, Fake, board, card
from .test_pump2 import repo as _repo  # noqa: F401

repo = _repo

ME = "devvybot-vps"
HEADER = re.compile(r"^### @(devvybot-vps)/(\w+) — \d{4}-\d\d-\d\dT", re.M)


def _bf(repo: Path, me: str | None = ME, **extra: object) -> Path:
    data: dict[str, object] = {"boards": [{"name": "t", "repo": str(repo)}]}
    if me is not None:
        data["me"] = me
    data.update(extra)
    p = repo / "boards.json"
    p.write_text(json.dumps(data))
    return p


def _assigned(repo: Path, cid: str, assignee: str, status: str = "in_progress") -> Path:
    p = card(repo, cid, status=status)
    p.write_text(
        p.read_text().replace("title: t\n", f"title: t\nassignee: {assignee}\n", 1)
    )
    return p


def _tick(repo: Path, tmp_path: Path, bf: Path, f: Fake) -> list[str]:
    return runner.tick(
        {"t": board(repo, "true")},
        boards_file=bf,
        launch_stage=f.launch,
        status=lambda j: (False, 0),
        output=lambda j, n: "",
        locks=tmp_path / "l",
    )


def _launched(repo: Path, tmp_path: Path, assignee: str, **extra: object) -> bool:
    (repo / TASKS / "dv-0001.md").unlink()
    _assigned(repo, "dv-0002", assignee)
    f = Fake()
    _tick(repo, tmp_path, _bf(repo, **extra), f)
    return bool(f.launched)


# --- C1: created_by comes from the install identity


def test_C1_pump2_new_has_no_by_flag(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    with pytest.raises(SystemExit):
        cli_main(
            ["--boards", str(bf), "new", "t", "--title", "x", "--by", "romanbot/devops"]
        )


def test_C1_pump2_new_stamps_install_and_profile(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    assert cli_main(["--boards", str(bf), "new", "t", "--title", "x"]) == 0
    cid = capsys.readouterr().out.split()[0].split(":")[1]
    c = read_card(repo / TASKS / f"{cid}.md")
    assert c.fm["created_by"] == f"{ME}/cli"


def test_C1_pump2_new_assignee_is_stored(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    assert (
        cli_main(
            ["--boards", str(bf), "new", "t", "--title", "x", "--assignee", "romanbot"]
        )
        == 0
    )
    cid = capsys.readouterr().out.split()[0].split(":")[1]
    c = read_card(repo / TASKS / f"{cid}.md")
    assert c.fm["assignee"] == "romanbot" and c.fm["created_by"].startswith(ME + "/")


def test_C1_identity_module_rejects_foreign_author() -> None:
    from src.pump2 import identity

    assert identity.author("devops", me=ME) == f"{ME}/devops"
    for bad in ("romanbot/devops", ME, "a b", ""):
        with pytest.raises(ValueError):
            identity.author(bad, me=ME)


# --- C2: comment headers are written by one writer


def test_C2_add_comment_header_carries_install(repo: Path, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    monkeypatch.setattr("src.pump2.state.DEFAULT_BOARDS_FILE", bf, raising=False)
    monkeypatch.setenv("PUMP2_BOARDS_FILE", str(bf))
    p = card(repo, "dv-0002")
    f = Fake()
    runner.tick(
        {"t": board(repo, "true")},
        boards_file=bf,
        launch_stage=f.launch,
        status=lambda j: (False, 0),
        output=lambda j, n: "",
        locks=tmp_path / "l",
    )
    body = read_card(p).body
    assert HEADER.search(body), body
    assert "### @pump2 —" not in body.split("old comment")[1]


def test_C2_owner_answer_header(repo: Path, tmp_path: Path) -> None:
    bf = _bf(repo)
    card(repo, "dv-0002", status="todo")
    assert cli_main(["--boards", str(bf), "start", "t:dv-0002"]) == 0
    body = read_card(repo / TASKS / "dv-0002.md").body
    m = HEADER.search(body)
    assert m and m.group(2) == "pump2"


# --- C3: is_ours / the tick filter


@pytest.mark.parametrize(
    "assignee,ours",
    [
        ("", True),
        (ME, True),
        (f"{ME}/devops", True),
        ("devops", True),  # board profile
        ("legacyname", True),  # me_aliases
        ("romanbot", False),
        ("romanbot/builder", False),
    ],
)
def test_C3_is_ours_table(assignee: str, ours: bool) -> None:
    from src.pump2 import identity

    assert (
        identity.is_ours(
            assignee, me=ME, local={"devops", "builder"}, aliases={"legacyname"}
        )
        is ours
    )


def test_C3_is_ours_me_unset_takes_everything() -> None:
    from src.pump2 import identity

    assert identity.is_ours("romanbot", me="", local=set(), aliases=set()) is True


def test_C3_tick_skips_other_install(repo: Path, tmp_path: Path) -> None:
    (repo / TASKS / "dv-0001.md").unlink()
    p = _assigned(repo, "dv-0002", "romanbot")
    before = p.read_bytes()
    f = Fake()
    _tick(repo, tmp_path, _bf(repo), f)
    assert f.launched == [] and p.read_bytes() == before


def test_C3_tick_skips_staged_card_of_other_install(repo: Path, tmp_path: Path) -> None:
    from src.pump2.state import set_pump2

    (repo / TASKS / "dv-0001.md").unlink()
    p = _assigned(repo, "dv-0002", "romanbot/builder")
    set_pump2(p, stage="analyze", attempt=0)
    before = p.read_bytes()
    f = Fake()
    _tick(repo, tmp_path, _bf(repo), f)
    assert f.launched == [] and p.read_bytes() == before


@pytest.mark.parametrize("who", [ME, f"{ME}/builder", "", "devops", "extra"])
def test_C3_tick_launches_ours(repo: Path, tmp_path: Path, who: str) -> None:
    if who:
        assert _launched(repo, tmp_path, who, me_aliases=["extra"])
    else:
        (repo / TASKS / "dv-0001.md").unlink()
        f = Fake()
        card(repo, "dv-0002")
        _tick(repo, tmp_path, _bf(repo), f)
        assert f.launched


def test_C3_local_profile_of_other_board_is_ours(repo: Path, tmp_path: Path) -> None:
    assert _launched(
        repo,
        tmp_path,
        "builder",
        boards=[
            {"name": "t", "repo": str(repo)},
            {"name": "g", "repo": "/x", "profile": "builder"},
        ],
    )


# --- C4: Theirs block


def test_C4_status_lists_theirs_apart(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _assigned(repo, "dv-0002", "romanbot", status="todo")
    _assigned(repo, "dv-0003", "romanbot", status="in_progress")
    bf = _bf(repo)
    assert cli_main(["--boards", str(bf), "status"]) == 0
    out = capsys.readouterr().out
    assert "Theirs" in out
    theirs = out.split("Theirs", 1)[1]
    assert "dv-0002" in theirs and "dv-0003" in theirs and "romanbot" in theirs
    assert "dv-0002" not in out.split("Theirs", 1)[0]


def test_C4_pin_renders_theirs_within_limit(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    for i in range(30):
        _assigned(repo, f"dv-1{i:03d}", "romanbot", status="in_progress")
    bf = _bf(repo)
    assert cli_main(["--boards", str(bf), "pin", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Theirs" in out and "romanbot" in out


# --- C5: backward compatibility


def test_C5_me_unset_takes_every_card(repo: Path, tmp_path: Path) -> None:
    (repo / TASKS / "dv-0001.md").unlink()
    _assigned(repo, "dv-0002", "romanbot")
    f = Fake()
    _tick(repo, tmp_path, _bf(repo, me=None), f)
    assert [x[0] for x in f.launched] == ["dv-0002"]


def test_C5_legacy_and_new_headers_side_by_side(repo: Path) -> None:
    from src.pump2.state import parse_comment_header

    assert parse_comment_header("### @pump2 — 2026-11-12T10:00:00Z") == (
        "",
        "pump2",
        "2026-11-12T10:00:00Z",
    )
    assert parse_comment_header(f"### @{ME}/owner — 2026-11-12T10:00:00Z") == (
        ME,
        "owner",
        "2026-11-12T10:00:00Z",
    )
    assert parse_comment_header("### @taskpump — 2026-11-12T10:00:00Z")[1] == "taskpump"


def test_C5_new_header_is_read_by_digest_and_pending(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    p = card(repo, "dv-0002")
    p.write_text(
        p.read_text()
        + "\n### @pump2 — 2026-11-12T10:00:00Z\nanalyze: plan ready\n"
        + "\n### @devvybot-vps/pump2 — 2026-11-12T11:00:00Z\n"
        + 'analyze: questions for the owner (answer with `pump2 answer t:dv-0002 "x"`)\n'
        + "1. A or B?\n   - (a) A\n   - (b) B\n"
    )
    from src.pump2.state import set_pump2, set_status

    set_pump2(p, stage="analyze", attempt=0)
    set_status(p, "waiting_user")
    bf = _bf(repo)
    assert cli_main(["--boards", str(bf), "pending", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any("dv-0002" in json.dumps(r) for r in rows)


def test_C5_card_without_fields_still_parses(repo: Path) -> None:
    cards, bad = list_cards(board(repo, "true"))
    assert cards and not bad
    assert not cards[0].fm.get("assignee")


# --- C7 / C8 / C9 (advice)


def test_C7_doctor_prints_identity(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    cli_main(["--boards", str(bf), "doctor"])
    assert f"me: {ME}" in capsys.readouterr().out


def test_C8_assign_moves_card_between_installs(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    p = card(repo, "dv-0002", status="todo")
    assert cli_main(["--boards", str(bf), "assign", "t:dv-0002", "romanbot"]) == 0
    assert read_card(p).fm["assignee"] == "romanbot"
    assert HEADER.search(read_card(p).body)
    assert cli_main(["--boards", str(bf), "assign", "t:dv-0002", "-"]) == 0
    assert not read_card(p).fm.get("assignee")
    assert cli_main(["--boards", str(bf), "assign", "t:dv-0002", "Bad Name"]) != 0


def test_C9_start_refuses_other_install(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    p = _assigned(repo, "dv-0002", "romanbot", status="todo")
    assert cli_main(["--boards", str(bf), "start", "t:dv-0002"]) != 0
    assert "romanbot" in capsys.readouterr().err
    assert read_card(p).fm["status"] == "todo"


def test_C11_status_json_has_no_theirs_in_work_groups(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    bf = _bf(repo)
    _assigned(repo, "dv-0002", "romanbot")
    assert cli_main(["--boards", str(bf), "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    for r in rows:
        if "dv-0002" in json.dumps(r):
            assert r.get("group") == "theirs"
