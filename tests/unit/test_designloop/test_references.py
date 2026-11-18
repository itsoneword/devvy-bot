"""dv-a5aa C1, C7: the board's references (README text + images) and the
owner's verdict log reach the designer and the critic."""

import json
from pathlib import Path
from typing import Any, List

import pytest

from src.designloop.steps import BoardSteps, DesignConfig, parse_config
from src.model_router import CallRequest, CallResult, set_router

from .test_steps import AGENTJOB, CARD, TEMPLATE, Runner
from .test_steps import repo as _shared_repo  # noqa: F401


@pytest.fixture
def repo(_shared_repo: Path) -> Path:  # noqa: F811
    """The shared `repo` fixture under its own name."""
    return _shared_repo


REFS_DIR = "docs/design-code/references"
README = (
    "# Owner references\n\nI like the calm Strava feed (strava.jpg) "
    "REFTEXT_MARKER and the Whoop card (whoop.png). Also see missing.png "
    "and readme-only.jpeg.\n"
)


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_router(None)


def _cfg(**kw: Any) -> DesignConfig:
    return parse_config(
        {
            "render": ["r"],
            "gate": ["g"],
            "python": "py",
            "references": f"{REFS_DIR}/README.md",
            **kw,
        }
    )


def _write_refs(repo: Path) -> Path:
    d = repo / REFS_DIR
    d.mkdir(parents=True)
    (d / "README.md").write_text(README, encoding="utf-8")
    (d / "strava.jpg").write_bytes(b"\xff\xd8\xffa")
    (d / "whoop.png").write_bytes(b"\x89PNGb")
    (d / "other.png").write_bytes(b"\x89PNGc")  # not named in the README
    return d


def test_C1_parse_config_reads_references_keys():
    cfg = _cfg(reference_images=2, verdicts="docs/v.md")
    assert cfg.references == f"{REFS_DIR}/README.md"
    assert cfg.reference_images == 2
    assert cfg.verdicts == "docs/v.md"
    assert _cfg().reference_images == 4
    assert _cfg(reference_images=99).reference_images == 8
    assert _cfg(reference_images=-3).reference_images == 0
    # no references key: the feature is off
    assert parse_config(
        {"render": ["r"], "gate": ["g"], "python": "py"}
    ).references in (
        None,
        "",
    )


class _Capture:
    def __init__(self) -> None:
        self.requests: List[CallRequest] = []

    async def complete(self, request: CallRequest) -> CallResult:
        self.requests.append(request)
        crit = {
            k: "ok"
            for k in __import__("src.designloop.critic", fromlist=["x"]).CRITERIA
        }
        return CallResult(
            text=json.dumps({"criteria": crit, "verdict": "ready", "score": 8})
        )


def _steps(repo: Path, tmp_path: Path, config: DesignConfig) -> BoardSteps:
    return BoardSteps(
        repo=repo,
        card=CARD,
        screen="feed",
        worktrees=tmp_path / "wt",
        out=repo / ".screens" / "design" / CARD,
        config=config,
        profile="app",
        agentjob=AGENTJOB,
        template=TEMPLATE,
        run=Runner(),
    )


async def _critic_request(repo, tmp_path, config, cap) -> CallRequest:
    from src.designloop.brief import build_brief

    set_router(cap)
    s = _steps(repo, tmp_path, config)
    s.current_dir.mkdir(parents=True, exist_ok=True)
    (s.current_dir / "main-dark.png").write_bytes(b"\x89PNGcur")
    render = tmp_path / "v1" / "main-dark.png"
    render.parent.mkdir(parents=True, exist_ok=True)
    render.write_bytes(b"\x89PNGren")
    brief = build_brief(repo, "feed", "calmer feed")
    await s.critic(brief, [render])
    return cap.requests[0]


async def test_C1_critic_gets_references_text_images_and_owner_criterion(
    repo, tmp_path  # noqa: F811
):
    _write_refs(repo)
    cap = _Capture()
    req = await _critic_request(repo, tmp_path, _cfg(reference_images=4), cap)
    names = [Path(p).name for p in req.images]
    assert "strava.jpg" in names and "whoop.png" in names
    assert "other.png" not in names and "readme-only.jpeg" not in names
    assert names.index("strava.jpg") < names.index("whoop.png")
    assert req.prompt.count("owner reference") >= 2
    assert "REFTEXT_MARKER" in req.prompt
    assert "main-dark.png" in " ".join(names)  # the current screen stays as before


async def test_C1_reference_images_are_capped_at_n(repo, tmp_path):  # noqa: F811
    _write_refs(repo)
    req = await _critic_request(repo, tmp_path, _cfg(reference_images=1), _Capture())
    assert [Path(p).name for p in req.images if "references" in str(p)] == [
        "strava.jpg"
    ]


async def test_C1_without_the_key_prompt_and_images_stay_as_today(
    repo, tmp_path  # noqa: F811
):
    _write_refs(repo)
    cfg = parse_config({"render": ["r"], "gate": ["g"], "python": "py"})
    req = await _critic_request(repo, tmp_path, cfg, _Capture())
    assert "owner reference" not in req.prompt
    assert "REFTEXT_MARKER" not in req.prompt
    assert not any("references" in str(p) for p in req.images)
    assert "the current screen, and the named" in req.prompt


async def test_C1_with_references_reference_match_is_judged_against_the_owner(
    repo, tmp_path  # noqa: F811
):
    _write_refs(repo)
    req = await _critic_request(repo, tmp_path, _cfg(), _Capture())
    line = next(
        ln for ln in req.prompt.splitlines() if ln.startswith("- reference_match")
    )
    assert "owner" in line and "verdict" in line
    assert "before" in line


def test_C1_generator_prompt_carries_the_references_text(repo, tmp_path):  # noqa: F811
    from src.designloop.brief import build_brief

    _write_refs(repo)
    s = _steps(repo, tmp_path, _cfg())
    brief = build_brief(repo, "feed", "calmer feed")
    assert "REFTEXT_MARKER" in s.prompt(brief, 1, ["design/x/v1", "design/x/v2"], ())


def test_C7_brief_says_names_in_the_problem_may_be_internal(repo):  # noqa: F811
    from src.designloop.brief import build_brief

    text = build_brief(repo, "feed", "rework activityPack").text.lower()
    assert "internal" in text and "user-facing" in text
