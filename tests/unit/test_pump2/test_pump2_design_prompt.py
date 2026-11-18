"""dv-b9ec: the builder prompt carries the card's open comments, a kit
component inventory generated from code, and tokens from the approved style
spec only. Read through ``pump2 design prompt`` (no model call, no card write)."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.pump2.state import write_comment
from tests.unit.test_designloop.test_mockup_mode import spec_text

from .test_pump2_design import DESIGN, DID, GD, fm  # noqa: F401
from .test_pump2_design import gym as _shared_gym  # noqa: F401
from .test_pump2_design import pump2, write_card  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
SPEC_NAME = "docs/style-spec.yaml"
COMMON = "webapp/src/common.tsx"

TSX = """\
import React from "react";

/** Organizer row with avatar and follow button. Second sentence. */
export function OrganizerCard(props: Props) {
  return null;
}

// Overflow menu behind a dots button.
export const MoreMenu = () => null;

export class Sparkle {}

export function helper() {}

function Private() {}
"""


@pytest.fixture
def gym(_shared_gym: Path) -> Path:  # noqa: F811
    return _shared_gym


def _configure(tmp_path: Path, **design: Any) -> None:
    f = tmp_path / "boards.json"
    data = json.loads(f.read_text())
    data["boards"][0]["design"] = {**DESIGN, **design}
    f.write_text(json.dumps(data))


def _prompt(tmp_path: Path, capsys: Any) -> str:
    capsys.readouterr()
    assert pump2(tmp_path, "design", "prompt", GD) == 0
    return capsys.readouterr().out


def _card(gym: Path) -> Path:
    return write_card(gym, DID, design_screen="feed")


def _write(gym: Path, rel: str, text: str) -> None:
    p = gym / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _comment(path: Path, profile: str, text: str) -> None:
    write_comment(path, text, profile)


def test_C1_prompt_has_non_pump_comments_oldest_first(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    path = _card(gym)
    _comment(path, "owner", "OWNER_NOTE covers path /x/covers-sketch")
    _comment(path, "pump2", "PUMP_REPORT run finished")
    _comment(path, "devops", "DEVOPS_NOTE build from OrganizerCard")
    before = path.read_bytes()
    out = _prompt(tmp_path, capsys)
    assert path.read_bytes() == before  # read-only
    assert "covers-sketch" in out and "DEVOPS_NOTE" in out
    assert out.index("OWNER_NOTE") < out.index("DEVOPS_NOTE")
    assert "PUMP_REPORT" not in out


def test_C1_no_comments_no_instructions_section(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    _card(gym)
    out = _prompt(tmp_path, capsys)
    assert "Instructions on the card" not in out


def test_C2_inventory_lists_exported_components(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    _card(gym)
    _write(gym, COMMON, TSX)
    _configure(tmp_path, components=[COMMON])
    out = _prompt(tmp_path, capsys)
    lines = out.splitlines()
    org = [ln for ln in lines if "OrganizerCard" in ln]
    assert org and COMMON in org[0] and "Organizer row with avatar" in org[0]
    assert "Second sentence" not in org[0]
    more = [ln for ln in lines if "MoreMenu" in ln]
    assert more and "Overflow menu" in more[0]
    assert any("Sparkle" in ln for ln in lines)
    assert not any("helper" in ln or "Private" in ln for ln in lines)


def test_C2_without_components_key_the_section_is_absent(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    _card(gym)
    _write(gym, COMMON, TSX)
    out = _prompt(tmp_path, capsys)
    assert "OrganizerCard" not in out


def test_C2_parse_config_reads_components() -> None:
    from src.designloop.steps import parse_config

    base = {"render": ["r"], "gate": ["g"], "python": "py"}
    assert parse_config({**base, "components": ["a.tsx"]}).components == ("a.tsx",)
    assert parse_config(base).components == ()
    assert parse_config({**base, "components": "a.tsx"}).components is None or (
        parse_config({**base, "components": "a.tsx"}) is None
    )


def _approved_spec(gym: Path, tmp_path: Path) -> None:
    _write(gym, SPEC_NAME, spec_text())
    _configure(tmp_path, style_spec=SPEC_NAME)


def test_C3_approved_spec_replaces_the_tokens_section(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    _card(gym)
    _approved_spec(gym, tmp_path)
    out = _prompt(tmp_path, capsys)
    assert "#7A9E7E" in out  # the spec's palette
    assert "Shared tokens (docs/SCREENS.md" not in out
    assert "Touch targets 44 / 38 / 32" not in out  # §1 prose gone
    assert "the spec wins" in out


def test_C3_without_approved_spec_section_one_stays(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    _card(gym)
    out = _prompt(tmp_path, capsys)
    assert "Shared tokens (docs/SCREENS.md" in out
    assert "Touch targets 44 / 38 / 32" in out
    # a draft spec is not used either
    _write(gym, SPEC_NAME, spec_text(status="draft"))
    _configure(tmp_path, style_spec=SPEC_NAME)
    out = _prompt(tmp_path, capsys)
    assert "Shared tokens (docs/SCREENS.md" in out


def test_C5_no_board_specific_names_in_pump_code() -> None:
    pat = re.compile(r"gymbro|OrganizerCard|common\.tsx|webapp/", re.I)
    hits = []
    for d in ("src/pump2", "src/designloop"):
        for f in (ROOT / d).rglob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}")
    assert hits == []


def test_C6_template_stays_under_budget() -> None:
    size = (ROOT / "config/taskpump/design_generate.md").stat().st_size
    assert size < 2048
    assert "{{COMPONENTS}}" in (ROOT / "config/taskpump/design_generate.md").read_text()


def test_C6_comments_and_inventory_are_capped(
    gym: Path, tmp_path: Path, capsys: Any
) -> None:
    path = _card(gym)
    for i in range(10):
        _comment(path, "owner", f"NOTE{i:02d} " + "x" * 700)
    big = "\n".join(f"export function Comp{i:03d}() {{}}" for i in range(100))
    _write(gym, COMMON, big)
    _configure(tmp_path, components=[COMMON])
    out = _prompt(tmp_path, capsys)
    assert "NOTE09" in out and "NOTE00" not in out  # newest kept
    comps = [ln for ln in out.splitlines() if re.search(r"Comp\d{3}", ln)]
    assert 0 < len(comps) <= 60
    assert re.search(r"\b40\b[^\n]*(cut|omitted|more)", out, re.I)


@pytest.mark.asyncio
async def test_C7_critic_prompt_holds_the_spec_once(tmp_path: Path) -> None:
    from src.designloop.brief import build_brief
    from src.designloop.critic import critique_mockup
    from src.model_router import set_router
    from src.model_router.types import CallResult
    from tests.unit.test_designloop.helpers import make_app_repo
    from tests.unit.test_designloop.test_mockup_mode import answer
    from tests.unit.test_designloop.test_references import _Capture

    class _MockupCapture(_Capture):
        async def complete(self, request: Any) -> CallResult:
            self.requests.append(request)
            return CallResult(text=answer())

    repo = make_app_repo(tmp_path)
    cap = _MockupCapture()
    set_router(cap)
    try:
        text = spec_text()
        brief = dataclasses.replace(build_brief(repo, "feed", "calmer"), spec=text)
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG")
        await critique_mockup(brief, [img], mockup=img, spec_text=text)
    finally:
        set_router(None)
    assert cap.requests[0].prompt.count("#7A9E7E") == 1
