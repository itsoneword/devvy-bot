"""Tests for scripts/bgjob (loaded as a module despite no .py suffix)."""

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BGJOB_PATH = Path(__file__).parents[2] / "scripts" / "bgjob"


@pytest.fixture
def bgjob():
    loader = SourceFileLoader("bgjob_under_test", str(_BGJOB_PATH))
    spec = importlib.util.spec_from_loader("bgjob_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def one_running_job(bgjob, tmp_path, monkeypatch):
    """A jobs root containing exactly one job whose unit reports active."""
    jid = "20260802-120000-research"
    d = tmp_path / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "id": jid,
                "name": "research",
                "profile": "general",
                "unit": f"bgjob-{jid}",
            }
        )
    )
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(bgjob, "unit_active", lambda unit: True)
    monkeypatch.setattr(bgjob, "job_progress", lambda unit: "")
    return jid


class TestRenderJobsStatus:
    def test_running_job_is_listed(self, bgjob, one_running_job):
        text = bgjob.render_jobs_status()
        assert "research" in text
        assert "(1)" in text

    def test_exclude_hides_the_finishing_job(self, bgjob, one_running_job):
        # run.sh refreshes the pin from INSIDE the finishing job's still-active
        # unit; --exclude is what stops the pin from listing it forever.
        assert bgjob.render_jobs_status(exclude=one_running_job) == ""

    def test_exclude_other_job_keeps_this_one(self, bgjob, one_running_job):
        assert "research" in bgjob.render_jobs_status(exclude="some-other-id")


class TestRunShTemplate:
    def test_run_sh_pins_with_exclude_and_appends_output_tail(
        self, bgjob, tmp_path, monkeypatch
    ):
        """cmd_run writes a run.sh that retires its own pin entry and ships
        the output tail in the finish message."""
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
        monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
        # Any existing executable will do -- cmd_run only checks existence.
        monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stderr = ""
                stdout = ""

            return R()

        monkeypatch.setattr(bgjob.subprocess, "run", fake_run)

        args = type(
            "A",
            (),
            {
                "prompt": "do it",
                "profile": "general",
                "name": "t",
                "allow_dir": None,
                # Added by the --model flag (20c2e18); the stub was not
                # updated, so this test has been failing since.
                "model": "sonnet",
            },
        )()
        bgjob.cmd_run(args)

        run_sh = next(tmp_path.glob("*/run.sh")).read_text()
        job_id = next(tmp_path.glob("*/run.sh")).parent.name
        assert f"pin --exclude {job_id}" in run_sh
        assert "tail -c 1500" in run_sh
        # Agentic digest turn fires only after the ✅ ping got through.
        assert "prompt " in run_sh
        assert "--profile general" in run_sh
