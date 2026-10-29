"""Critic calibration on 3 current gymbro screens (dv-9133 phase 2 step 5).

Contract: ``src.designloop.calibrate.calibrate(repo, report)`` (sync or
async; ``scripts/designloop_calibrate`` wraps it) critiques the current refs
of feed/main, event_page/other and profile_tab/main (dark + light) against
industry references (Strava, Meetup/Partiful, Airbnb) through the router's
``design_critic`` route, and writes the raw critic output per screen to
``report`` (docs/research/dv-9133/calibration.md in the real run, after
merge: owner answer 2.1). The router here is a fake: no real Codex call.
"""

import inspect
import json
from pathlib import Path
from typing import Any, List

import pytest

from src.model_router import CallRequest, CallResult, ModelRouter, set_router
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository
from tests.unit.test_designloop.helpers import conforming, ref

SCREENS = (("feed", "main"), ("event_page", "other"), ("profile_tab", "main"))


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture(autouse=True)
def _reset_router():
    yield
    set_router(None)


def _screen_of(request: CallRequest) -> str:
    """Which calibration screen a call is about, from its images."""
    for screen, _ in SCREENS:
        if any(f"/screens/{screen}/" in str(p) for p in request.images):
            return screen
    raise AssertionError(f"no calibration screen in {request.images}")


async def test_calibration_report_holds_raw_critic_output_for_the_three_screens(
    gymbro_repo, tmp_path, db
):
    from src.designloop.calibrate import calibrate

    calls: List[CallRequest] = []

    async def codex(request: CallRequest) -> CallResult:
        calls.append(request)
        note = f"NOTE-{_screen_of(request)}-cards-misaligned"
        return CallResult(text=json.dumps(conforming(request.json_schema or {}, note)))

    async def claude(request: CallRequest) -> CallResult:
        raise AssertionError("calibration never runs on Claude")

    set_router(
        ModelRouter(
            plain_callers={"openai_codex": codex, "claude": claude},
            recorder=ModelCallRepository(db),
            versions=_Versions(),
        )
    )
    report = tmp_path / "calibration.md"

    result: Any = calibrate(gymbro_repo, report)
    if inspect.isawaitable(result):
        await result

    # One critic call per screen, each with that screen's dark + light ref.
    assert sorted(_screen_of(r) for r in calls) == sorted(s for s, _ in SCREENS)
    for request in calls:
        screen = _screen_of(request)
        state = dict(SCREENS)[screen]
        images = {str(Path(p).resolve()) for p in request.images}
        for theme in ("dark", "light"):
            assert str(ref(gymbro_repo, screen, state, theme).resolve()) in images
        assert request.purpose == "design_critic"

    # Industry references stand in for the owner's favourites.
    by_screen = {_screen_of(r): r.prompt + r.system_prompt for r in calls}
    assert "Strava" in by_screen["feed"]
    assert "Meetup" in by_screen["event_page"] or "Partiful" in by_screen["event_page"]
    assert "Airbnb" in "".join(by_screen.values())

    # The raw critic output for every screen is in the report.
    text = report.read_text(encoding="utf-8")
    for screen, state in SCREENS:
        assert screen in text
        assert f"NOTE-{screen}-cards-misaligned" in text
    assert text.count('"verdict"') >= 3

    rows = await _rows(db)
    assert len(rows) == 3
    assert {(r["purpose"], r["harness"]) for r in rows} == {
        ("design_critic", "openai_codex")
    }


async def _rows(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


def test_calibration_script_exists_and_is_executable():
    script = Path(__file__).resolve().parents[3] / "scripts" / "designloop_calibrate"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
