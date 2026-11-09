"""dv-97a4: while an ask waits on the owner, the job dir holds a marker the
pump reads for its "merge running" ping; it is gone once the ask returns."""

import json
from pathlib import Path

from src.model_router.job import ASK_MARKER, BotLink


async def test_ask_writes_the_marker_while_waiting_and_removes_it(
    tmp_path: Path,
) -> None:
    marker = tmp_path / ASK_MARKER
    link = BotLink("http://127.0.0.1:1", "s", ask_marker=marker)
    seen = {}

    def post(path, body, timeout):  # type: ignore[no-untyped-def]
        seen.update(json.loads(marker.read_text()))
        return {"approved": True}

    link.post = post  # type: ignore[method-assign]
    assert await link.ask("git reset --hard", "x" * 500, "pump-merge") is True
    assert seen["reason"] == "git reset --hard"
    assert len(seen["preview"]) == 200 and seen["since"].endswith("Z")
    assert not marker.exists()


async def test_marker_is_removed_when_the_bot_cannot_be_reached(
    tmp_path: Path,
) -> None:
    marker = tmp_path / ASK_MARKER

    def post(path, body, timeout):  # type: ignore[no-untyped-def]
        assert marker.exists()
        raise OSError("down")

    link = BotLink("http://127.0.0.1:1", "s", ask_marker=marker)
    link.post = post  # type: ignore[method-assign]
    assert await link.ask("r", "p", "s") is False
    assert not marker.exists()


async def test_unwritable_marker_never_fails_the_ask(tmp_path: Path) -> None:
    link = BotLink(
        "http://127.0.0.1:1", "s", ask_marker=tmp_path / "missing" / ASK_MARKER
    )
    link.post = lambda *a: {"approved": True}  # type: ignore[method-assign]
    assert await link.ask("r", "p", "s") is True


async def test_no_marker_without_a_job_dir() -> None:
    link = BotLink("http://127.0.0.1:1", "s")
    link.post = lambda *a: {"approved": False}  # type: ignore[method-assign]
    assert link.ask_marker is None
    assert await link.ask("r", "p", "s") is False
