"""The critic's own rules beyond the router contract (dv-9133 phase 2):
parsing, the "issues repeat" stop rule, image order and the calibration CLI."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.designloop.brief import build_brief, load_features, state_refs
from src.designloop.critic import CRITERIA, _images, parse, repeated
from src.model_router import ModelCallError

REPO_ROOT = Path(__file__).resolve().parents[3]


def _answer(verdict="revise", **fixes):
    criteria = {name: fixes.get(name, "ok") for name in CRITERIA}
    return json.dumps({"criteria": criteria, "score": 6, "verdict": verdict})


def test_parse_keeps_fixes_and_score():
    found = parse(_answer(hierarchy="fix: the bell chip repeats the bell button"))
    assert found.verdict == "revise" and found.score == 6
    assert found.fixes == {"hierarchy": "fix: the bell chip repeats the bell button"}


def test_parse_refuses_an_empty_fix():
    with pytest.raises(ModelCallError, match="hierarchy"):
        parse(_answer(hierarchy="fix:"))


def test_same_fixes_twice_repeat_new_ones_do_not():
    first = parse(_answer(density="fix: Drop the  bell chip"))
    again = parse(_answer(density="fix: drop the bell chip"))
    other = parse(_answer(density="fix: move Saved into the view switch"))
    assert repeated(first, again)
    assert not repeated(first, other)
    clean = parse(_answer(verdict="ready"))
    assert not repeated(clean, clean)


def test_images_put_renders_first_and_never_send_a_path_twice(gymbro_repo):
    brief = build_brief(gymbro_repo, "feed", "calmer")
    renders = state_refs(gymbro_repo, "feed", "main")
    images, legend = _images(renders, brief.refs, ())
    assert len(images) == len(set(images)) == len(brief.refs)
    assert legend[0].endswith("under review: feed/main-dark.png")
    assert all("current screen" in line for line in legend[len(renders) :])


def test_feature_parser_reads_inline_lists_and_ignores_scenarios(gymbro_repo):
    screens = load_features(gymbro_repo / "docs" / "features.yaml")
    join = {f.name: f for f in screens["event_page"].features}["join_leave"]
    assert join.action == "I'm going"
    assert join.api == ("POST /api/events/{id}/join", "POST /api/events/{id}/leave")
    assert screens["feed"].passport == "TAB Feed"
    assert "id" not in {f.name for f in screens["feed"].features}


def test_unknown_passport_is_an_error(gymbro_repo):
    passports = gymbro_repo / "docs" / "SCREENS.md"
    passports.write_text(
        passports.read_text().replace("### TAB Feed", "### TAB Other"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="TAB Feed"):
        build_brief(gymbro_repo, "feed", "calmer")


def test_calibration_script_help_runs():
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "designloop_calibrate"), "-h"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "--point-by-point" in out.stdout
