"""scripts/bgjob and the shared usage-limit record (dv-7ad9 phase 2).

- `bgjob summarize` (run.sh calls it after every job, pump jobs included)
  records the limit when the job ended on it: exit 4 plus agentjob's quota
  line in `stderr.log` -> `quota.record()`; `quota.active()` sees it.
- `bgjob run` of a model (prompt) job is refused while `quota.active()`,
  naming the reset time; `--force` starts it anyway. `--cmd` jobs make no
  model call and are never refused.
- run.sh's finish ping for exit 4 says the job stopped on the usage limit
  (with the reset time), not `FAILED (rc=4)`, and warns when the job's tree
  is left mid-merge (the 26.10 merge job left MERGE_HEAD in the live tree).

The record lives under the data root: the tests point `DEVVY_DATA_ROOT` at a
tmp dir. Fixture idiom of test_bgjob.py (the script loaded as a module, no
systemd).
"""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.test_bgjob import bgjob  # noqa: F401  (fixture)

LIMIT = "You've hit your weekly limit · resets 9am (UTC)"
T0 = datetime(2026, 10, 26, 8, 15, tzinfo=UTC)
RESET = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DEVVY_DATA_ROOT", str(data))
    return data


def _record_until(until: datetime) -> None:
    """A limit hit recorded by some earlier job, resetting at `until`."""
    from src.model_router import quota

    quota.record(quota.Limit(kind="weekly limit", until=until, message=LIMIT), "test")


def _args(**extra):
    base = {
        "prompt": "do it",
        "profile": "general",
        "name": "t",
        "allow_dir": None,
        "model": "sonnet",
    }
    base.update(extra)
    return type("A", (), base)()


def _patch_run(bgjob, jobs: Path, cwd: Path, monkeypatch) -> list:
    """cmd_run without systemd: profiles -> cwd, jobs root -> jobs. Returns
    the list of subprocess.run argvs (systemd-run among them)."""
    jobs.mkdir(exist_ok=True)
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(cwd)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")
    calls: list = []

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(
        bgjob.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R()
    )
    return calls


def _started(calls: list) -> bool:
    return any(c and c[0] == "systemd-run" for c in calls)


# --------------------------------------------------------------------------
# Scenario: any job ending on the limit records it until reset
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [
        {"name": "merge-devvy-review-26-10", "profile": "devops"},
        {"name": "pump-gb-1a71-implement", "profile": "gymbro", "quiet": True},
    ],
    ids=["owner-job", "pump-job"],
)
def test_summarize_of_a_job_ending_on_the_limit_records_it(
    bgjob, root, tmp_path, monkeypatch, meta
):
    from src.model_router import quota

    jobs = tmp_path / "jobs"
    jid = "20261026-081300-" + meta["name"]
    d = jobs / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": jid, **meta}))
    (d / "output.md").write_text("")
    limit = quota.parse_limit(LIMIT, T0)
    assert limit is not None
    (d / "stderr.log").write_text(
        "some log line\n" f"agentjob failed: {LIMIT}\n" + quota.quota_line(limit) + "\n"
    )
    (d / "exit_code").write_text("4\n")
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)

    assert quota.active(T0) is None
    assert bgjob.cmd_summarize(type("A", (), {"id": jid})()) == 0

    got = quota.active(T0)
    assert got is not None, "summarize did not record the limit"
    assert got.until == RESET
    # Expires by itself.
    assert quota.active(RESET + timedelta(minutes=1)) is None


def test_summarize_of_an_ordinary_failure_records_nothing(
    bgjob, root, tmp_path, monkeypatch
):
    """Guard: exit 1 without a limit line is a failure, not a limit."""
    from src.model_router import quota

    jobs = tmp_path / "jobs"
    d = jobs / "j"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": "j", "name": "j"}))
    (d / "output.md").write_text("")
    (d / "stderr.log").write_text("agentjob failed: API overloaded\n")
    (d / "exit_code").write_text("1\n")
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)

    bgjob.cmd_summarize(type("A", (), {"id": "j"})())
    assert quota.active(T0) is None


# --------------------------------------------------------------------------
# Scenario: bgjob run of a model job while exhausted is refused
# --------------------------------------------------------------------------


def test_model_job_is_refused_with_the_reset_time_while_exhausted(
    bgjob, root, tmp_path, monkeypatch
):
    until = (datetime.now(UTC) + timedelta(hours=2)).replace(second=0, microsecond=0)
    _record_until(until)
    jobs = tmp_path / "jobs"
    calls = _patch_run(bgjob, jobs, tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        bgjob.cmd_run(_args(name="merge-devvy"))

    msg = str(exc.value.code)
    assert "limit" in msg.lower(), msg
    assert until.strftime("%H:%M") in msg, msg
    assert "--force" in msg, msg
    assert not _started(calls), calls
    assert not list(jobs.glob("*/run.sh"))

    # --force: the owner tries anyway.
    assert bgjob.cmd_run(_args(name="merge-devvy", force=True)) == 0
    assert _started(calls)
    assert list(jobs.glob("*/run.sh"))


def test_expired_record_and_cmd_jobs_are_not_refused(
    bgjob, root, tmp_path, monkeypatch
):
    """added (guards): a record past its reset blocks nothing; a --cmd job
    (no model call) runs even while the limit is active."""
    _record_until(datetime.now(UTC) - timedelta(minutes=1))
    jobs = tmp_path / "jobs"
    calls = _patch_run(bgjob, jobs, tmp_path, monkeypatch)
    assert bgjob.cmd_run(_args(name="after-reset")) == 0
    assert _started(calls)

    _record_until(datetime.now(UTC) + timedelta(hours=2))
    calls.clear()
    ns = _args(name="full-tests", prompt=None, cmd="true", quiet=True, queue=False)
    assert bgjob.cmd_run(ns) == 0
    assert _started(calls)


# --------------------------------------------------------------------------
# Scenario: a merge job dying on the limit pings "stopped on limit, mid-merge"
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> int:
    # Popen-based helpers only: tests patch subprocess.run module-wide.
    return subprocess.call(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _repo(path: Path, mid_merge: bool) -> Path:
    path.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@t"),
        ("config", "user.name", "t"),
    ):
        assert _git(path, *args) == 0
    (path / "a.txt").write_text("base\n")
    assert _git(path, "add", "-A") == 0
    assert _git(path, "commit", "-qm", "base") == 0
    if mid_merge:
        assert _git(path, "checkout", "-qb", "feature") == 0
        (path / "a.txt").write_text("feature\n")
        assert _git(path, "commit", "-qam", "feature") == 0
        assert _git(path, "checkout", "-q", "main") == 0
        (path / "a.txt").write_text("main\n")
        assert _git(path, "commit", "-qam", "main") == 0
        assert _git(path, "merge", "feature") != 0  # conflict: stays mid-merge
        assert (path / ".git" / "MERGE_HEAD").exists()
    return path


def _finish_ping(run_sh: Path, rc: int, cwd: Path, env: dict) -> str:
    """Run run.sh's own finish-report block as emitted (from `d=$((` up to
    the send loop's `sent=0`) in the job's cwd with exit code `rc`; return
    the built $msg. The send loop itself (agentctl) never runs."""
    lines = run_sh.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("d=$(("))
    end = next(i for i in range(start, len(lines)) if lines[i] == "sent=0")
    block = "\n".join(lines[start:end])
    script = f"rc={rc}\nt0=$(date +%s)\nstarted_h=now\n{block}\nprintf '%s' \"$msg\"\n"
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    return out.decode("utf-8")


def _limit_job(bgjob, tmp_path, monkeypatch, cwd: Path, until: datetime):
    """A non-quiet prompt job in `cwd` whose agentjob ended on the limit."""
    from src.model_router import quota

    jobs = tmp_path / "jobs"
    _patch_run(bgjob, jobs, cwd, monkeypatch)
    assert bgjob.cmd_run(_args(name="merge-devvy-review-26-10")) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    d = run_sh.parent
    limit = quota.Limit(kind="weekly limit", until=until, message=LIMIT)
    (d / "stderr.log").write_text(
        f"agentjob failed: {LIMIT}\n" + quota.quota_line(limit) + "\n"
    )
    (d / "output.md").write_text("")
    (d / "exit_code").write_text("4\n")
    env = {
        **os.environ,
        "BGJOB_ROOT": str(jobs),
        "DEVVY_DATA_ROOT": os.environ["DEVVY_DATA_ROOT"],
    }
    return run_sh, env


def test_merge_job_dying_on_the_limit_pings_stopped_on_limit_and_mid_merge(
    bgjob, root, tmp_path, monkeypatch
):
    repo = _repo(tmp_path / "devvy_bot", mid_merge=True)
    until = (datetime.now(UTC) + timedelta(hours=3)).replace(second=0, microsecond=0)
    run_sh, env = _limit_job(bgjob, tmp_path, monkeypatch, repo, until)

    msg = _finish_ping(run_sh, 4, repo, env)

    first = msg.splitlines()[0]
    assert "limit" in first.lower(), msg
    assert "FAILED" not in msg and "✅" not in first, msg
    assert until.strftime("%H:%M") in msg, msg
    assert "mid-merge" in msg, msg
    assert str(repo) in msg, msg


def test_limit_ping_in_a_clean_tree_has_no_mid_merge_warning(
    bgjob, root, tmp_path, monkeypatch
):
    """The same limit stop in a tree with no merge in progress: the stop is
    reported, no mid-merge warning."""
    repo = _repo(tmp_path / "clean", mid_merge=False)
    until = (datetime.now(UTC) + timedelta(hours=3)).replace(second=0, microsecond=0)
    run_sh, env = _limit_job(bgjob, tmp_path, monkeypatch, repo, until)

    msg = _finish_ping(run_sh, 4, repo, env)

    assert "limit" in msg.splitlines()[0].lower(), msg
    assert "FAILED" not in msg
    assert "mid-merge" not in msg
