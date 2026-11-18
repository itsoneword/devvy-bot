"""dv-696e: the design critic returns every remark per block.

Contract pinned here (mockup mode):

- ``critic.Remark(block, element, mockup, render, change, kind)``: frozen
  dataclass, ``kind`` is ``build`` | ``data``; ``Critique.remarks`` is a tuple
  of them (default ``()``).
- The remarks call answers ``{"remarks": [{block, element, mockup, render,
  change, kind}]}``; ``dimensions`` calls keep the existing schema.
- ``notes_of`` adds ``<block> / <element>: mockup = …; render = …; change = …``
  for every ``build`` remark, uncapped; ``data`` remarks are left out.
- ``BoardSteps.critic`` writes ``<renders dir>/critic.json``.
- ``src.designloop.crops.make_crops(png, sidecar, out_dir)`` (Pillow, run by
  the board's python) crops copies; originals stay untouched.
- ``run_loop`` in mockup mode stops with ``stalled`` when round n did not
  raise the lowest dimension over round n-1 (n >= 3).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.designloop.critic import CRITERIA, Critique

from .test_mockup_mode import answer

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


def remark(i, kind="build", block="hero"):
    from src.designloop.critic import Remark

    return Remark(
        block=block,
        element=f"el{i}",
        mockup=f"mock{i}",
        render=f"rend{i}",
        change=f"css{i}",
        kind=kind,
    )


def mockup_critique(score=5, remarks=(), fix=""):
    from src.designloop.critic import DIMENSIONS

    criteria = {k: "ok" for k in CRITERIA}
    if fix:
        criteria["hierarchy"] = f"fix: {fix}"
    return Critique(
        verdict="revise",
        criteria=criteria,
        score=score,
        dimensions={d: (score, f"the {d} differs") for d in DIMENSIONS},
        threshold=7,
        remarks=tuple(remarks),
    )


# ---------------------------------------------------------------- C1


def test_C1_notes_hold_every_build_remark_uncapped():
    from src.designloop.loop import notes_of

    c = mockup_critique(remarks=[remark(i) for i in range(45)], fix="bigger title")
    notes = notes_of(c)
    for i in range(45):
        assert (
            f"hero / el{i}: mockup = mock{i}; render = rend{i}; change = css{i}"
            in notes
        )
    assert any(n.startswith("hierarchy:") and "bigger title" in n for n in notes)
    assert any(n.startswith("palette 5/10") for n in notes)


async def test_C1_remarks_reach_the_builders_round_two_feedback(tmp_path):
    from src.designloop.loop import run_loop

    from .test_loop import Steps

    steps = Steps(
        tmp_path,
        verdicts=lambda n, b: mockup_critique(
            5 + n, remarks=[remark(i) for i in range(8)], fix=f"fix round {n}"
        ),
    )
    brief = SimpleNamespace(screen_id="feed", text="b", refs=(), owner_led=False)
    await run_loop(brief, **steps.kwargs())
    lines = [ln for fb in steps.feedback[1].values() for ln in fb]
    assert sum("hero / el" in ln and "change = css" in ln for ln in lines) >= 8


# ---------------------------------------------------------------- C2


def _sidecar(path: Path, blocks, scale=2, content_height=100):
    path.write_text(
        json.dumps({"scale": scale, "content_height": content_height, "blocks": blocks})
    )


def test_C2_crops_are_copies_cut_from_sidecar_boxes(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from src.designloop.crops import make_crops

    png = tmp_path / "main-dark.png"
    Image.new("RGB", (200, 1000), (255, 0, 0)).save(png)
    before = png.read_bytes()
    _sidecar(
        tmp_path / "main-dark.boxes.json",
        [
            {"id": "hero", "x": 0, "y": 10, "w": 50, "h": 20},
            {"id": "outside", "x": 0, "y": 900, "w": 50, "h": 20},
            {"id": "empty", "x": 5, "y": 5, "w": 0, "h": 20},
        ],
    )
    out = tmp_path / "critic"
    result = make_crops(png, tmp_path / "main-dark.boxes.json", out)

    assert png.read_bytes() == before
    assert Image.open(result["page"]).size == (200, 200)  # content_height * scale
    assert set(result["blocks"]) == {"hero"}
    assert Image.open(result["blocks"]["hero"]).size == (100, 40)
    assert out in Path(result["blocks"]["hero"]).parents
    assert {s.split(":")[0] for s in result["skipped"]} == {"outside", "empty"}
    assert not list(tmp_path.glob("hero*.png"))  # nothing new beside the original


# ---------------------------------------------------------------- C3 / C4


def _remarks_answer(items):
    return json.dumps({"remarks": items})


def _item(i, kind="build", block="hero"):
    return {
        "block": block,
        "element": f"el{i}",
        "mockup": f"mock{i}",
        "render": f"rend{i}",
        "change": f"css{i}",
        "kind": kind,
    }


async def _run_critic(monkeypatch, tmp_path, items):
    from src.designloop import critic as critic_mod

    from .test_steps import AGENTJOB, CONFIG, TEMPLATE, BoardSteps, Runner

    seen = []

    class R:
        async def complete(self, request):
            seen.append(request)
            schema = json.dumps(request.json_schema)
            if '"dimensions"' in schema:
                return SimpleNamespace(text=answer({"palette": 4}))
            return SimpleNamespace(text=_remarks_answer(items))

    monkeypatch.setattr(critic_mod, "get_router", lambda: R())
    repo = tmp_path / "repo"
    repo.mkdir()
    s = BoardSteps(
        repo=repo,
        card="dv-d001",
        screen="feed",
        worktrees=tmp_path / "wt",
        out=tmp_path / "out",
        config=CONFIG,
        profile="app",
        agentjob=AGENTJOB,
        template=TEMPLATE,
        run=Runner(),
    )
    (tmp_path / "current").mkdir()
    s.__dict__["current_pngs"] = []
    mock = tmp_path / "m.png"
    mock.write_bytes(PNG)
    s.use_mockup(mock, tmp_path / "spec.md", "SPEC")
    folder = tmp_path / "out" / "v1" / "a2"
    folder.mkdir(parents=True)
    render = folder / "main-dark.png"
    render.write_bytes(PNG)
    brief = SimpleNamespace(text="brief", refs=(), owner_led=False)
    try:
        c = await s.critic(brief, [render])
    except Exception:
        # BoardSteps wiring differs (owner inputs): the test is about
        # critic.json, so surface the real failure.
        raise
    return c, folder, seen


async def test_C3_critic_json_saved_per_round(monkeypatch, tmp_path):
    items = [_item(1), _item(2, "data")]
    c, folder, seen = await _run_critic(monkeypatch, tmp_path, items)
    data = json.loads((folder / "critic.json").read_text())
    assert data["round"] == 2
    for key in ("images", "answers", "dimensions", "scores", "criteria", "remarks"):
        assert key in data
    assert "notes" in data
    assert data["images"] and data["answers"]
    assert {r["kind"] for r in data["remarks"]} == {"build", "data"}
    assert any("palette 4/10" in n for n in data["notes"])
    assert not list(folder.glob("*.png"))[1:]  # only the render itself


async def test_C4_data_remarks_saved_but_not_sent_or_in_verdict(monkeypatch, tmp_path):
    from src.designloop.loop import notes_of

    items = [_item(1), _item(2, "data"), _item(3, "data")]
    c, folder, _ = await _run_critic(monkeypatch, tmp_path, items)
    notes = " ".join(notes_of(c))
    assert "el1" in notes and "el2" not in notes and "el3" not in notes
    assert [r.kind for r in c.remarks].count("data") == 2
    saved = json.loads((folder / "critic.json").read_text())["notes"]
    assert not any("el2" in n for n in saved)
    assert c.verdict == "revise" and c.score == 4  # remarks do not move the score


def test_C4_data_remarks_are_not_in_notes():
    from src.designloop.loop import notes_of

    c = mockup_critique(remarks=[remark(1), remark(2, "data")])
    assert not any("el2" in n for n in notes_of(c))


# ---------------------------------------------------------------- C5


async def test_C5_round_three_skipped_when_lowest_dimension_did_not_rise(tmp_path):
    from src.designloop.loop import run_loop

    from .test_loop import Steps

    steps = Steps(
        tmp_path,
        verdicts=lambda n, b: mockup_critique(5, fix=f"different fix {n}"),
    )
    brief = SimpleNamespace(screen_id="feed", text="b", refs=(), owner_led=False)
    outcome = await run_loop(brief, **steps.kwargs())
    assert outcome.stop == "stalled"
    assert outcome.rounds == 2
    assert outcome.offered  # offered like "rounds"
    assert "stalled" not in outcome.message or outcome.message
    assert "lowest" in outcome.message.lower() or "raise" in outcome.message.lower()


async def test_C5_round_three_runs_when_lowest_dimension_rose(tmp_path):
    from src.designloop.loop import run_loop

    from .test_loop import Steps

    steps = Steps(
        tmp_path,
        verdicts=lambda n, b: mockup_critique(min(4 + n, 6), fix=f"fix {n}"),
    )
    brief = SimpleNamespace(screen_id="feed", text="b", refs=(), owner_led=False)
    outcome = await run_loop(brief, **steps.kwargs())
    assert outcome.rounds == 3 and outcome.stop == "rounds"


# ---------------------------------------------------------------- C6


async def test_C6_remarks_call_goes_through_the_router_as_design_critic(
    monkeypatch, tmp_path
):
    _, _, seen = await _run_critic(monkeypatch, tmp_path, [_item(1)])
    assert len(seen) >= 2  # dimensions + at least one remarks call
    assert {r.purpose for r in seen} == {"design_critic"}
