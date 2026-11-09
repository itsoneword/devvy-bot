"""``pump2 design run`` / ``pending`` / ``answer`` for designloop cards
(dv-9133, moved from the old pump by dv-81a7). Ported from the deleted
test_taskpump_design_run.py, test_taskpump_designloop.py and
test_taskpump_designloop_pick.py. The loop is the real ``run_loop``; its
steps are faked (``BoardSteps`` replaced): no git, model or browser."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import src.designloop.steps as steps_mod
import src.model_router.job as job_mod
from src.designloop.critic import CRITERIA, Critique
from src.pump2 import cli, design
from src.pump2.state import Board, hold_reason, read_card
from tests.unit.test_designloop.helpers import make_app_repo

DID = "dv-d001"
BID = "dv-b001"
GD, GB = f"app:{DID}", f"app:{BID}"
BRANCHES = [f"design/{DID}/v{n}" for n in (1, 2, 3)]
DESIGN = {"render": ["render", "{out}"], "gate": ["gate"], "python": "py"}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(design, "send", lambda argv: calls.append(list(argv)))
    monkeypatch.setattr(job_mod, "install_job_router", lambda root: None)
    return calls


def boards_file(tmp_path: Path, repo: Path, with_design: bool = True) -> Path:
    b: dict[str, Any] = {"name": "app", "repo": str(repo), "profile": "app"}
    if with_design:
        b["design"] = DESIGN
    f = tmp_path / "boards.json"
    f.write_text(json.dumps({"boards": [b]}))
    return f


@pytest.fixture
def gym(tmp_path: Path) -> Path:
    repo = make_app_repo(tmp_path)
    (repo / ".deviz" / "tasks").mkdir(parents=True)
    boards_file(tmp_path, repo)
    return repo


def write_card(repo: Path, cid: str, **keys: str) -> Path:
    fm = {
        "id": cid,
        "title": f"card {cid}",
        "status": "in_progress",
        "labels": "[ui, designloop]",
        **keys,
    }
    lines = [f"{k}: {v}" for k, v in fm.items() if v != ""]
    p = repo / ".deviz" / "tasks" / f"{cid}.md"
    p.write_text(
        "---\n" + "\n".join(lines) + "\n---\n\n## Description\n\nA calmer feed.\n"
    )
    return p


def fm(path: Path, key: str) -> str:
    v = read_card(path).fm.get(key)
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return "" if v is None else str(v)


def pump2(tmp_path: Path, *argv: str) -> int:
    return cli.main(["--boards", str(tmp_path / "boards.json"), *argv])


def fake_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: str
) -> dict[str, Any]:
    made: dict[str, Any] = {}

    class Steps:
        def __init__(self, **kw: Any) -> None:
            made.update(kw)

        def prepare(self) -> list[str]:
            return []

        def kwargs(self) -> dict[str, Any]:
            async def generate(brief: Any, feedback: Any) -> list[str]:
                made["brief"] = brief
                return list(BRANCHES)

            async def render(branch: str) -> list[Path]:
                path = tmp_path / branch.replace("/", "_") / "main-dark.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\x89PNG")
                return [path]

            async def gate(branch: str, renders: Any) -> list[str]:
                return []

            async def critic(brief: Any, renders: Any) -> Critique:
                fix = (
                    ""
                    if verdict == "ready"
                    else f"fix: {renders[0].parent.name} bell twice"
                )
                criteria = {k: "ok" for k in CRITERIA}
                if fix:
                    criteria["density"] = fix
                return Critique(verdict=verdict, criteria=criteria, score=7)

            async def collage(variants: Any) -> Path:
                path = tmp_path / "collage.png"
                path.write_bytes(b"\x89PNG collage")
                return path

            return dict(
                generate=generate,
                render=render,
                gate=gate,
                critic=critic,
                collage=collage,
            )

    monkeypatch.setattr(steps_mod, "BoardSteps", Steps)
    return made


def awaiting(repo: Path) -> Path:
    write_card(
        repo,
        BID,
        labels="[ui]",
        deps=f"[{DID}]",
    )
    return write_card(
        repo,
        DID,
        design_state="awaiting_pick",
        design_variants="[" + ", ".join(BRANCHES) + "]",
    )


def board_of(repo: Path) -> Board:
    return Board(name="app", repo=repo, profile="app", design=DESIGN)


# --- design run -----------------------------------------------------------


def test_design_run_ready_awaits_a_pick_and_sends_one_collage(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list, capsys: Any
) -> None:
    path = write_card(gym, DID, design_screen="feed")
    made = fake_steps(monkeypatch, tmp_path, "ready")

    assert pump2(tmp_path, "design", "run", GD, "--by", "devops") == 0

    assert made["brief"].screen_id == "feed"
    assert made["profile"] == "app" and made["card"] == DID
    assert fm(path, "design_state") == "awaiting_pick"
    assert fm(path, "design_variants") == "[" + ", ".join(BRANCHES) + "]"
    assert fm(path, "status") == "in_progress"
    assert "Design run started by devops." in path.read_text()
    with_image = [c for c in sent if "--image" in c]
    assert len(with_image) == 1
    cmd = with_image[0]
    assert cmd[cmd.index("--image") + 1] == str(tmp_path / "collage.png")
    assert cmd[cmd.index("--profile") + 1] == "app" and "--deliver" in cmd
    capsys.readouterr()
    assert pump2(tmp_path, "pending") == 0
    assert "DESIGN PICK" in capsys.readouterr().out
    assert pump2(tmp_path, "answer", GD, "3") == 0
    assert fm(path, "design_branch") == BRANCHES[2]


def test_design_run_without_ready_offers_with_remarks(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list, capsys: Any
) -> None:
    """dv-2d00: no ready variant is still a pick, the remarks go with it
    (the gate-only failure: test_pump2_design_not_ready.py)."""
    path = write_card(gym, DID, design_screen="feed")
    fake_steps(monkeypatch, tmp_path, "revise")

    assert pump2(tmp_path, "design", "run", GD) == 0

    assert fm(path, "design_state") == "awaiting_pick"
    assert len([c for c in sent if "--image" in c]) == 1
    assert any("recommended" in c[2] and "bell twice" in c[2] for c in sent)
    assert "bell twice" in path.read_text()
    capsys.readouterr()
    pump2(tmp_path, "pending")
    assert DID in capsys.readouterr().out


@pytest.mark.parametrize(
    "keys, why",
    [
        ({"labels": "[ui, design]"}, "designloop"),
        ({"design_screen": ""}, "design_screen"),
        ({"design_state": "awaiting_pick"}, "past its run"),
        ({"design_screen": "nope"}, "no brief"),
    ],
    ids=["not-designloop", "no-screen", "already-offered", "unknown-screen"],
)
def test_design_run_refuses_and_leaves_the_card_unchanged(
    gym: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sent: list,
    capsys: Any,
    keys: dict[str, str],
    why: str,
) -> None:
    path = write_card(gym, DID, **{"design_screen": "feed", **keys})
    before = path.read_bytes()
    fake_steps(monkeypatch, tmp_path, "ready")

    assert pump2(tmp_path, "design", "run", GD) == 2

    assert why in capsys.readouterr().err
    assert path.read_bytes() == before
    assert sent == []


def test_board_without_a_design_block_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list, capsys: Any
) -> None:
    repo = make_app_repo(tmp_path)
    (repo / ".deviz" / "tasks").mkdir(parents=True)
    boards_file(tmp_path, repo, with_design=False)
    path = write_card(repo, DID, design_screen="feed")
    before = path.read_bytes()

    assert pump2(tmp_path, "design", "run", GD) == 2

    assert "design" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_a_failed_send_keeps_the_card_state_and_prints_the_error(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """No outbox in pump2: the card holds the state, the error is printed."""
    monkeypatch.setattr(job_mod, "install_job_router", lambda root: None)

    def down(argv: list[str]) -> None:
        raise RuntimeError("bot down")

    monkeypatch.setattr(design, "send", down)
    path = write_card(gym, DID, design_screen="feed")
    fake_steps(monkeypatch, tmp_path, "ready")

    assert pump2(tmp_path, "design", "run", GD) == 0

    assert fm(path, "design_state") == "awaiting_pick"
    assert "bot down" in capsys.readouterr().err


# --- the pump leaves designloop cards alone --------------------------------


@pytest.mark.parametrize("status", ["todo", "in_progress"])
def test_start_refuses_a_designloop_card(
    gym: Path, tmp_path: Path, capsys: Any, status: str
) -> None:
    path = write_card(gym, DID, status=status)
    before = path.read_bytes()
    capsys.readouterr()

    assert pump2(tmp_path, "start", GD) == 2

    out = capsys.readouterr()
    msg = out.out + out.err
    assert "designloop" in msg
    assert len([ln for ln in msg.splitlines() if ln.strip()]) == 1
    assert path.read_bytes() == before


def test_urgent_refuses_a_designloop_card(
    gym: Path, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.pump2 import freeze

    called: list[Any] = []
    monkeypatch.setattr(freeze, "urgent", lambda *a: called.append(a))
    path = write_card(gym, DID)
    before = path.read_bytes()

    assert pump2(tmp_path, "urgent", GD) == 2

    assert path.read_bytes() == before and called == []


def test_a_design_topic_label_is_still_started(gym: Path, tmp_path: Path) -> None:
    path = write_card(gym, BID, labels="[events, ui, design]", status="todo")

    assert pump2(tmp_path, "start", GB) == 0

    assert read_card(path).pump2["stage"] == "analyze"
    assert "pump:" not in path.read_text()


def test_tick_never_launches_a_designloop_card(gym: Path, tmp_path: Path) -> None:
    from src.pump2 import runner

    write_card(gym, DID, design_screen="feed")
    lines = runner.tick({"app": board_of(gym)}, launch=_no_launch)
    assert not [ln for ln in lines if DID in ln]


def _no_launch(*a: Any, **k: Any) -> str:  # pragma: no cover - must not run
    raise AssertionError("launched")


def test_status_does_not_list_a_designloop_card(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    write_card(gym, DID)
    capsys.readouterr()
    assert pump2(tmp_path, "status") == 0
    assert DID not in capsys.readouterr().out


# --- the pick ---------------------------------------------------------------


def test_pick_of_reply() -> None:
    assert design.pick_of_reply("2") == 2
    assert design.pick_of_reply(f"{DID}: 2") == 2
    assert design.pick_of_reply("v3", 3) == 3
    assert design.pick_of_reply("7", 3) is None
    assert design.pick_of_reply("0") is None
    assert design.pick_of_reply("ok") is None


@pytest.mark.parametrize("reply", ["2", f"{DID}: 2"], ids=["bare", "with-id"])
def test_reply_2_closes_the_card_and_meets_the_build_dep(
    gym: Path, tmp_path: Path, capsys: Any, reply: str
) -> None:
    path = awaiting(gym)
    build = read_card(gym / ".deviz" / "tasks" / f"{BID}.md")
    assert DID in hold_reason(build, board_of(gym))

    capsys.readouterr()
    assert pump2(tmp_path, "pending") == 0
    assert DID in capsys.readouterr().out
    assert pump2(tmp_path, "answer", GD, reply) == 0

    assert fm(path, "status") == "done"
    assert fm(path, "design_state") == "picked"
    assert fm(path, "design_pick") == "v2"
    assert fm(path, "design_branch") == BRANCHES[1]
    text = path.read_text()
    assert "### @owner" in text and reply in text
    assert "### @pump2" in text and "Design picked: 2" in text
    assert hold_reason(build, board_of(gym)) == ""


@pytest.mark.parametrize("reply", ["the second one looks nice", "7", "0", "ok"])
def test_a_reply_that_is_not_a_variant_number_changes_nothing(
    gym: Path, tmp_path: Path, capsys: Any, reply: str
) -> None:
    path = awaiting(gym)
    before = path.read_bytes()
    build_before = (gym / ".deviz" / "tasks" / f"{BID}.md").read_bytes()
    capsys.readouterr()

    assert pump2(tmp_path, "answer", GD, reply) == 2

    said = capsys.readouterr().err
    assert re.search(r"\b1\b", said) and re.search(r"\b3\b", said), said
    assert path.read_bytes() == before
    assert (gym / ".deviz" / "tasks" / f"{BID}.md").read_bytes() == build_before
    pump2(tmp_path, "pending")
    assert DID in capsys.readouterr().out


def test_a_second_pick_is_refused_and_keeps_the_first(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    path = awaiting(gym)
    assert pump2(tmp_path, "answer", GD, "2") == 0
    after = path.read_bytes()

    assert pump2(tmp_path, "answer", GD, "3") == 2

    assert path.read_bytes() == after
    assert fm(path, "design_pick") == "v2"
    capsys.readouterr()
    pump2(tmp_path, "pending")
    assert DID not in capsys.readouterr().out


def test_pending_json_has_a_pick_record(gym: Path, tmp_path: Path, capsys: Any) -> None:
    awaiting(gym)
    capsys.readouterr()
    pump2(tmp_path, "pending", "--json")
    recs = json.loads(capsys.readouterr().out)
    (rec,) = [r for r in recs if r["id"] == GD]
    assert rec["kind"] == "pick"
    assert [q["num"] for q in rec["questions"]] == ["1", "2", "3"]
    assert BRANCHES[0] in rec["block"]


def test_C3_fresh_started_variants_reach_card_and_owner(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list
) -> None:
    path = write_card(gym, DID, design_screen="feed")
    fake_steps(monkeypatch, tmp_path, "ready")
    base_cls = steps_mod.BoardSteps

    class Fresh(base_cls):  # type: ignore[valid-type, misc]
        def prepare(self) -> list[str]:
            self.fresh = ["v2 started fresh: merging abc1234 conflicted"]
            return []

    monkeypatch.setattr(steps_mod, "BoardSteps", Fresh)

    assert pump2(tmp_path, "design", "run", GD) == 0

    assert "v2 started fresh: merging abc1234 conflicted" in path.read_text()
    assert any("v2 started fresh" in " ".join(c) for c in sent)
