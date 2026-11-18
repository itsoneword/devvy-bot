# flake8: noqa: F811  (pytest fixtures imported from test_pump2_design)
"""dv-75f1 wiring around the mockup-mode modules: the pump2 run's step 0,
the owner's mockup pick, the spec's seed/approve status, doctor and the
designer prompt. Contracts of the modules themselves: test_mockup_mode.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from src.designloop import mockups as mockups_mod
from src.designloop.stylespec import parse_spec, set_status
from src.pump2 import design, doctor
from src.pump2.state import Board
from tests.unit.test_designloop.test_mockup_mode import SPEC, spec_text
from tests.unit.test_pump2.test_pump2_design import (  # noqa: F401
    DESIGN,
    DID,
    GD,
    fm,
    gym,
    pump2,
    sent,
    write_card,
)

SPEC_FILE = "docs/style-spec.yaml"
MOCKUP_DESIGN = dict(DESIGN, style_spec=SPEC_FILE, mockups={"count": 3})


def _board(repo: Path, tmp_path: Path, status: str = "approved") -> Board:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / SPEC_FILE).write_text(set_status(spec_text(), status))
    b = {"name": "app", "repo": str(repo), "profile": "app", "design": MOCKUP_DESIGN}
    (tmp_path / "boards.json").write_text(json.dumps({"boards": [b]}))
    return Board(name="app", repo=repo, profile="app", design=MOCKUP_DESIGN)


def _config() -> Any:
    from src.designloop.steps import parse_config

    return parse_config(MOCKUP_DESIGN)


# --- spec status ------------------------------------------------------------


def test_set_status_flips_only_the_status_line() -> None:
    text = spec_text()
    approved = set_status(set_status(text, "draft"), "approved")
    assert parse_spec(approved).approved
    assert yaml.safe_load(approved) == SPEC


def test_draft_or_missing_spec_gives_no_mockup_step(gym: Path, tmp_path: Path) -> None:
    _board(gym, tmp_path, "draft")
    assert mockups_mod.mockup_step(config=_config(), repo=gym) is None
    assert "draft" in mockups_mod.spec_problem(gym, SPEC_FILE)
    (gym / SPEC_FILE).unlink()
    assert mockups_mod.mockup_step(config=_config(), repo=gym) is None
    assert "missing" in mockups_mod.spec_problem(gym, SPEC_FILE)


def test_approved_spec_gives_the_step(gym: Path, tmp_path: Path) -> None:
    _board(gym, tmp_path)
    step = mockups_mod.mockup_step(config=_config(), repo=gym)
    assert step is not None and step.count == 3 and step.preset == "best"


# --- C9: FLUX down -> the kit flow with one note -----------------------------


def test_C9_no_mockup_keeps_the_kit_flow_with_a_note(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(gym, tmp_path)
    card_path = write_card(gym, DID, design_screen="feed")
    from src.pump2.state import read_card

    async def boom(**kw: Any) -> Any:
        raise mockups_mod.MockupError("no mockup drawn: flux call failed: 502")

    monkeypatch.setattr(mockups_mod, "make_mockups", boom)
    steps = SimpleNamespace(out=tmp_path / "out")
    brief = SimpleNamespace(text="brief")
    mode, notes, mockup, drawn = asyncio.run(
        design._step0(board, read_card(card_path), _config(), brief, steps, GD)
    )
    assert mode == "off" and mockup is None and drawn == []
    assert len(notes) == 1 and "kit flow" in notes[0] and "502" in notes[0]


# --- C7: the owner's mockup pick ---------------------------------------------


def test_C7_owner_label_waits_and_a_number_resumes(
    gym: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sent: list,  # noqa: F811
    capsys: Any,
) -> None:
    _board(gym, tmp_path)
    path = write_card(
        gym, DID, design_screen="feed", labels="[ui, designloop, ownermockup]"
    )
    drawn = []
    for n in (1, 2, 3):
        p = tmp_path / f"mockup-{n}.png"
        p.write_bytes(b"\x89PNG")
        drawn.append(p)

    async def make(**kw: Any) -> list:
        return drawn

    monkeypatch.setattr(mockups_mod, "make_mockups", make)
    monkeypatch.setattr(design, "_sheet", lambda *a: None)
    assert pump2(tmp_path, "design", "run", GD) == 0
    assert fm(path, "design_state") == "awaiting_mockup"
    assert any("mockup" in c[2] for c in sent)
    capsys.readouterr()
    assert pump2(tmp_path, "pending") == 0
    assert "MOCKUP PICK" in capsys.readouterr().out

    started: list = []
    monkeypatch.setattr(design, "resume_run", lambda b, f: started.append(f) or "")
    assert pump2(tmp_path, "answer", GD, "nope") == 2
    assert pump2(tmp_path, "answer", GD, "2") == 0
    assert fm(path, "design_state") == "mockup_picked"
    assert fm(path, "design_mockup") == str(drawn[1])
    assert started == [GD]


def test_owner_mockup_file_sets_the_pick(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``design run --mockup FILE`` (owner 13.11): the run builds to his file."""
    _board(gym, tmp_path)
    path = write_card(gym, DID, design_screen="feed", labels="[ui, designloop]")
    pick = tmp_path / "sprint" / "feed-1.png"
    pick.parent.mkdir()
    pick.write_bytes(b"\x89PNG")
    ran: list = []
    monkeypatch.setattr(design, "cmd_design", lambda b, f, by: ran.append(f) or 0)
    assert pump2(tmp_path, "design", "run", GD, "--mockup", str(tmp_path / "x")) == 2
    assert ran == []
    assert pump2(tmp_path, "design", "run", GD, "--mockup", str(pick)) == 0
    given = Path(fm(path, "design_mockup"))
    assert fm(path, "design_state") == "mockup_picked"
    assert given.is_file() and given.parent.name == "owner-mockup"
    assert ran == [GD]


# --- C10: the designer prompt -------------------------------------------------


def test_C10_designer_prompt_names_mockup_and_spec(tmp_path: Path) -> None:
    from src.designloop.steps import BoardSteps

    steps = BoardSteps(
        repo=tmp_path,
        card="dv-x",
        screen="feed",
        worktrees=tmp_path / "wt",
        out=tmp_path / "out",
        config=_config(),
        profile="app",
        agentjob=tmp_path / "agentjob",
        template=tmp_path / "t.md",
    )
    assert steps.mockup_section() == "" and steps.variant_count == 3
    steps.use_mockup(tmp_path / "m.png", tmp_path / SPEC_FILE, "SPEC")
    section = steps.mockup_section()
    assert str(tmp_path / "m.png") in section and SPEC_FILE in section
    assert "stays code" in section and steps.variant_count == 1


# --- C11: doctor ------------------------------------------------------------------


def test_C11_doctor_reports_draft_spec_and_missing_flux_keys(
    gym: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(gym, tmp_path, "draft")
    import src.model_router.routes as routes

    monkeypatch.setattr(routes, "flux_credentials", lambda: None)
    out = doctor._check_mockups(_config(), gym)
    assert any("draft" in line for line in out)
    assert any("FLUX_API_TOKEN" in line for line in out)
    monkeypatch.setattr(routes, "flux_credentials", lambda: ("t", "u"))
    (gym / SPEC_FILE).write_text(spec_text())
    assert doctor._check_mockups(_config(), gym) == []


def test_invalid_spec_refuses_the_run(
    gym: Path, tmp_path: Path, sent: list
) -> None:  # noqa: F811
    _board(gym, tmp_path)
    (gym / SPEC_FILE).write_text(spec_text(palette=None))
    path = write_card(gym, DID, design_screen="feed")
    assert pump2(tmp_path, "design", "run", GD) == 2
    assert fm(path, "design_state") == ""
