"""dv-2d00: ``pump2 design run`` when no variant is ready."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import src.designloop.steps as steps_mod
from src.designloop.critic import CRITERIA, Critique
from tests.unit.test_pump2.test_pump2_design import (  # noqa: F401
    BRANCHES,
    DID,
    GD,
    fm,
    gym,
    pump2,
    sent,
    write_card,
)

SCORES = dict(zip(BRANCHES, (6, 7, 8)))


def steps_with(monkeypatch, tmp_path: Path, gate_fails: bool) -> None:
    class Steps:
        def __init__(self, **kw: Any) -> None:
            pass

        def prepare(self) -> list[str]:
            return []

        def kwargs(self) -> dict[str, Any]:
            async def generate(brief: Any, feedback: Any) -> list[str]:
                return list(BRANCHES)

            async def render(branch: str) -> list[Path]:
                p = tmp_path / branch.replace("/", "_") / "main-dark.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\x89PNG")
                return [p]

            async def gate(branch: str, renders: Any) -> list[str]:
                return ["clipped at 390 px"] if gate_fails else []

            async def critic(brief: Any, renders: Any) -> Critique:
                branch = "design/%s/%s" % (DID, renders[0].parent.name.split("_")[-1])
                crit = {k: "ok" for k in CRITERIA}
                crit["density"] = f"fix: {branch} needs a Browse action"
                return Critique("revise", crit, score=SCORES[branch])

            async def collage(variants: Any) -> Path:
                p = tmp_path / "collage.png"
                p.write_bytes(b"\x89PNG collage")
                return p

            return dict(
                generate=generate,
                render=render,
                gate=gate,
                critic=critic,
                collage=collage,
            )

    monkeypatch.setattr(steps_mod, "BoardSteps", Steps)


def test_C3_not_ready_run_awaits_a_pick_in_score_order(
    gym, tmp_path, monkeypatch, sent, capsys
):
    path = write_card(gym, DID, design_screen="feed")
    steps_with(monkeypatch, tmp_path, gate_fails=False)

    assert pump2(tmp_path, "design", "run", GD) == 0

    assert fm(path, "design_state") == "awaiting_pick"
    assert fm(path, "design_variants") == "[" + ", ".join(reversed(BRANCHES)) + "]"
    assert len([c for c in sent if "--image" in c]) == 1
    capsys.readouterr()
    assert pump2(tmp_path, "pending") == 0
    assert "DESIGN PICK" in capsys.readouterr().out
    assert pump2(tmp_path, "answer", GD, "1") == 0
    assert fm(path, "design_branch") == BRANCHES[2]


def test_C4_gate_only_failure_stays_failed_without_image(
    gym, tmp_path, monkeypatch, sent, capsys
):
    path = write_card(gym, DID, design_screen="feed")
    steps_with(monkeypatch, tmp_path, gate_fails=True)

    assert pump2(tmp_path, "design", "run", GD) == 0

    assert fm(path, "design_state") == "failed"
    assert fm(path, "design_variants") == ""
    assert not [c for c in sent if "--image" in c]
    assert any("design failed" in c[2] for c in sent)
