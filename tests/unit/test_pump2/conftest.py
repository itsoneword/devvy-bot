"""Every pump2 test: the pin refresh a tick runs (dv-5c22) never reaches the
real bot, the model router or this repo's data/pump2/, and no tick launches a
real bgjob (06.11: since dv-79f0 a tick takes a fixture's in_progress card, and
test runs started real analyze / merge jobs on /tmp repos)."""

from pathlib import Path
from typing import Any

import pytest

from src.pump2 import pin, runner


@pytest.fixture(autouse=True)
def _offline_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_model(prompt: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(pin, "send", lambda argv: None)
    monkeypatch.setattr(pin, "_complete", no_model)
    monkeypatch.setattr(pin, "STATE_DIR", tmp_path / "pin-state")
    monkeypatch.setattr(pin, "home_started", lambda: None)
    fake = tmp_path / "no-bgjob"
    fake.write_text("import sys\nsys.exit('pump2 tests launch no real bgjob')\n")
    monkeypatch.setattr(runner, "BGJOB", fake)
