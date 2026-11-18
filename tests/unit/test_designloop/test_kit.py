"""dv-0aa5: the board's UI kit (``design.kit``): images generated once outside
the loop, attached first to the critic, named to the designer."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.designloop.brief import build_brief
from src.designloop.steps import parse_config
from src.pump2.template import fill_template

from .test_references import (  # noqa: F401
    REFS_DIR,
    _Capture,
    _cfg,
    _critic_request,
    _reset,
    _steps,
    _write_refs,
)
from .test_steps import repo as _shared_repo  # noqa: F401


@pytest.fixture
def repo(_shared_repo: Path) -> Path:  # noqa: F811
    """The shared `repo` fixture under its own name."""
    return _shared_repo


KIT = [f"{REFS_DIR}/gen-ui-kit-dark-1.jpg", f"{REFS_DIR}/gen-ui-kit-light-1.jpg"]
BASE = {"render": ["r"], "gate": ["g"], "python": "py"}
ROOT = Path(__file__).resolve().parents[3]


def _base() -> str:
    # HEAD's template carries {{KIT}} once the build commit lands: use main's.
    proc = subprocess.run(
        ["git", "merge-base", "HEAD", "main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:  # public export / tarball: no history to compare
        pytest.skip("no git history with a main branch")
    return proc.stdout.strip()


def _write_kit(repo: Path, names=KIT) -> None:  # noqa: F811
    for n in names:
        p = repo / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8\xffkit")


def test_C1_parse_config_reads_kit():
    assert parse_config({**BASE, "kit": KIT}).kit == tuple(KIT)
    assert parse_config(BASE).kit == ()
    assert parse_config({**BASE, "kit": []}).kit == ()
    six = [f"k{i}.png" for i in range(6)]
    assert parse_config({**BASE, "kit": six}).kit == tuple(six)


@pytest.mark.parametrize(
    "kit",
    [
        "a.png",
        [1],
        ["a.txt"],
        ["a.png", None],
        [f"k{i}.png" for i in range(7)],
        {"a": 1},
    ],
)
def test_C1_bad_kit_makes_the_block_malformed(kit: Any):
    assert parse_config({**BASE, "kit": kit}) is None


async def test_C2_kit_attached_after_current_before_references(
    repo, tmp_path
):  # noqa: F811
    _write_refs(repo)
    _write_kit(repo)
    req = await _critic_request(repo, tmp_path, _cfg(kit=KIT), _Capture())
    names = [Path(p).name for p in req.images]
    kit_i = [names.index(Path(k).name) for k in KIT]
    assert kit_i == sorted(kit_i) and len(kit_i) == 2
    assert names.index("main-dark.png") < min(kit_i)
    assert max(kit_i) < names.index("strava.jpg") < names.index("whoop.png")
    assert "UI kit" in req.prompt


async def test_C2_kit_does_not_use_reference_slots_and_is_attached_once(
    repo, tmp_path
):  # noqa: F811
    _write_refs(repo)
    _write_kit(repo)
    # the README also names a kit file: attached once, no README slot used
    readme = repo / REFS_DIR / "README.md"
    readme.write_text(
        "REFTEXT_MARKER gen-ui-kit-dark-1.jpg strava.jpg whoop.png\n", encoding="utf-8"
    )
    req = await _critic_request(
        repo, tmp_path, _cfg(kit=KIT, reference_images=2), _Capture()
    )
    names = [Path(p).name for p in req.images]
    assert names.count("gen-ui-kit-dark-1.jpg") == 1
    assert "strava.jpg" in names and "whoop.png" in names


async def test_C3_reference_match_judges_kit_first(repo, tmp_path):  # noqa: F811
    _write_refs(repo)
    _write_kit(repo)
    req = await _critic_request(repo, tmp_path, _cfg(kit=KIT), _Capture())
    line = next(
        ln for ln in req.prompt.splitlines() if ln.startswith("- reference_match")
    )
    assert "UI kit" in line
    for word in ("button", "chip", "tab", "card", "badge"):
        assert word in line
    assert line.index("UI kit") < line.index("verdict")


async def test_C3_without_kit_criterion_unchanged(repo, tmp_path):  # noqa: F811
    _write_refs(repo)
    req = await _critic_request(repo, tmp_path, _cfg(), _Capture())
    assert "UI kit" not in req.prompt


async def test_C3_kit_without_references_file_is_owner_led(
    repo, tmp_path
):  # noqa: F811
    _write_kit(repo)
    cfg = parse_config({**BASE, "kit": KIT})
    req = await _critic_request(repo, tmp_path, cfg, _Capture())
    assert "UI kit" in req.prompt
    assert any(Path(p).name == "gen-ui-kit-dark-1.jpg" for p in req.images)


def _prompt(repo, tmp_path, cfg):  # noqa: F811
    s = _steps(repo, tmp_path, cfg)
    brief = build_brief(repo, "feed", "calmer feed")
    branches = ["design/x/v1", "design/x/v2"]
    return s, s.prompt(brief, 1, branches, ())


def test_C4_designer_prompt_lists_kit_paths(repo, tmp_path):  # noqa: F811
    _write_refs(repo)
    _write_kit(repo)
    s, prompt = _prompt(repo, tmp_path, _cfg(kit=KIT))
    assert str(repo / KIT[0]) in prompt and str(repo / KIT[1]) in prompt
    assert "UI kit" in prompt


def test_C4_kit_file_committed_in_the_worktree_is_named_there(
    repo, tmp_path
):  # noqa: F811
    _write_refs(repo)
    _write_kit(repo)
    s = _steps(repo, tmp_path, _cfg(kit=KIT))
    wt = s.worktree("design/x/v1")
    _write_kit(wt)
    brief = build_brief(repo, "feed", "calmer feed")
    prompt = s.prompt(brief, 1, ["design/x/v1", "design/x/v2"], ())
    assert str(wt / KIT[0]) in prompt


def test_C4_without_kit_prompt_is_byte_identical_to_before(
    repo, tmp_path
):  # noqa: F811
    _write_refs(repo)
    s, prompt = _prompt(repo, tmp_path, _cfg())
    old = subprocess.run(
        ["git", "show", f"{_base()}:config/taskpump/design_generate.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    brief = s.with_owner(build_brief(repo, "feed", "calmer feed"))
    values = {
        "BRIEF": brief.text,
        "VARIANT": "1",
        "COUNT": "2",
        "BRANCH": "design/x/v1",
        "WORKTREE": str(s.worktree("design/x/v1")),
        "NOTES_FILE": "DESIGN_NOTES.md",
        "OTHERS": "(none written yet)",
        "FEEDBACK": "(first draft: no feedback yet)",
        "KIT": "",  # main's template carries {{KIT}} since dv-0aa5 landed
        "MOCKUP": "",  # and {{MOCKUP}} since dv-75f1 landed
        "COMPONENTS": "",  # and {{COMPONENTS}} (kit components list)
    }
    assert prompt == fill_template(old, values)
    assert "UI kit" not in prompt


def test_C4_template_stays_within_the_2kb_budget():
    assert (ROOT / "config/taskpump/design_generate.md").stat().st_size <= 2048


def test_C6_collage_stays_render_only():
    import inspect

    from src.designloop import steps

    src = inspect.getsource(steps.BoardSteps.collage)
    assert "kit" not in src.lower()


def test_C8_critic_still_one_design_critic_call(repo, tmp_path):  # noqa: F811
    import asyncio

    _write_refs(repo)
    _write_kit(repo)
    cap = _Capture()
    asyncio.run(_critic_request(repo, tmp_path, _cfg(kit=KIT), cap))
    assert len(cap.requests) == 1 and cap.requests[0].purpose == "design_critic"
