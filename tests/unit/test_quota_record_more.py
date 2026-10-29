"""dv-7ad9 phase 2, beyond the protected tests: agentjob refuses at start
while a limit is recorded (no model call), bgjob --force passes that through,
and an earlier reset never shortens an active record."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.test_bgjob import bgjob  # noqa: F401  (fixture)
from tests.unit.test_bgjob_quota import _args, _patch_run

REPO = Path(__file__).resolve().parents[2]
LIMIT = "You've hit your weekly limit · resets 9am (UTC)"


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    return data


def _record(until: datetime) -> None:
    from src.model_router import quota

    quota.record(quota.Limit(kind="weekly limit", until=until, message=LIMIT), "t")


def test_an_earlier_reset_does_not_shorten_an_active_record(root) -> None:
    from src.model_router import quota

    later = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=5)
    _record(later)
    _record(later - timedelta(hours=4))
    got = quota.active()
    assert got is not None and got.until == later


def test_agentjob_refuses_at_start_while_the_limit_is_recorded(root, tmp_path) -> None:
    from src.model_router import quota

    until = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    _record(until)
    prompt = tmp_path / "p.txt"
    prompt.write_text("hi")
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "agentjob"),
            "--profile",
            "nobody",
            "--prompt-file",
            str(prompt),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "DEVVY_DATA_ROOT": str(root),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
    )
    assert proc.returncode == quota.QUOTA_EXIT, proc.stderr
    got = quota.find_limit(proc.stderr)
    assert got is not None and got.until == until, proc.stderr
    assert proc.stdout == ""


def test_force_passes_ignore_quota_to_agentjob(
    bgjob, root, tmp_path, monkeypatch  # noqa: F811
) -> None:
    _record(datetime.now(UTC) + timedelta(hours=2))
    jobs = tmp_path / "jobs"
    _patch_run(bgjob, jobs, tmp_path, monkeypatch)
    assert bgjob.cmd_run(_args(name="forced", force=True)) == 0
    run_sh = next(jobs.glob("*/run.sh")).read_text()
    assert "--ignore-quota" in run_sh


def test_a_job_ending_ok_records_nothing(
    bgjob, root, tmp_path, monkeypatch  # noqa: F811
):
    """Exit 0 with a quoted limit text in stderr is not a limit hit."""
    from src.model_router import quota

    d = tmp_path / "jobs" / "j"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"id": "j", "name": "j"}')
    (d / "output.md").write_text("")
    (d / "stderr.log").write_text(f"agentjob failed: {LIMIT}\n")
    (d / "exit_code").write_text("0\n")
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path / "jobs")
    bgjob.cmd_summarize(type("A", (), {"id": "j"})())
    assert quota.active() is None
