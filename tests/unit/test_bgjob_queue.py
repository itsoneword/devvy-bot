"""dv-5432: the run.sh side of `bgjob run --queue` and how a queued job shows.

test_bgjob.py pins the wait-slot / live_jobs contract; this file runs the
generated run.sh for real (with wait-slot stubbed) so the timeout path is
proven to skip the command and record rc 75.
"""

import importlib.util
import json
import re
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# bgjob.subprocess IS the subprocess module: the fixture's fakes patch it
# globally, so the real run.sh is started through the original.
_real_popen = subprocess.Popen
_BGJOB_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bgjob"


@pytest.fixture
def bgjob():
    loader = SourceFileLoader("bgjob_queue_under_test", str(_BGJOB_PATH))
    spec = importlib.util.spec_from_loader("bgjob_queue_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def box(bgjob, tmp_path, monkeypatch):
    """Jobs root with one running job, cap 1, fake systemd-run/notify."""
    root = tmp_path / "jobs"
    root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    active = set()
    calls: list = []
    monkeypatch.setattr(bgjob, "JOBS_ROOT", root)
    monkeypatch.setattr(bgjob, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(work)})
    monkeypatch.setattr(bgjob, "unit_active", lambda unit: unit in active)
    monkeypatch.setattr(bgjob, "job_progress", lambda unit: "")

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd and cmd[0] == "systemd-run":
            unit = next(a for a in cmd if a.startswith("--unit="))
            active.add(unit.split("=", 1)[1])
        return R()

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)

    other = "20261025-120000-research"
    (root / other).mkdir()
    (root / other / "meta.json").write_text(
        json.dumps({"id": other, "name": "research", "unit": f"bgjob-{other}"})
    )
    active.add(f"bgjob-{other}")

    class Box:
        pass

    b = Box()
    b.root, b.work, b.active, b.calls, b.other = root, work, active, calls, other
    return b


def _run(bgjob, *argv):
    import sys

    old = sys.argv
    sys.argv = ["bgjob", *argv]
    try:
        return bgjob.main()
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = old


def _new_job(box):
    (d,) = [p for p in box.root.iterdir() if p.name != box.other]
    return d


def _exec_run_sh(d: Path, wait_slot: str) -> None:
    """Run the job's run.sh with `wait-slot` replaced by `wait_slot`."""
    text = (d / "run.sh").read_text()
    text, n = re.subn(r"\S+ \S+ wait-slot \S+", wait_slot, text)
    assert n == 1, text
    # summarize would reach the real bgjob and its real jobs root.
    text = "\n".join(ln for ln in text.splitlines() if " summarize " not in ln)
    (d / "run.sh").write_text(text + "\n")
    _real_popen(["/bin/bash", str(d / "run.sh")]).wait(timeout=30)


def test_slot_timeout_skips_the_command_and_records_rc_75(bgjob, box):
    assert _run(bgjob, "run", "--quiet", "--queue", "--cmd", "touch ran") == 0
    d = _new_job(box)
    _exec_run_sh(d, "sh -c 'echo no job slot in 2h >&2; exit 75'")
    assert (d / "exit_code").read_text().strip() == "75"
    assert not (box.work / "ran").exists(), "command ran without a slot"
    assert "no job slot in 2h" in (d / "output.md").read_text()


def test_slot_taken_runs_the_command(bgjob, box):
    assert _run(bgjob, "run", "--quiet", "--queue", "--cmd", "touch ran") == 0
    d = _new_job(box)
    _exec_run_sh(d, "true")
    assert (d / "exit_code").read_text().strip() == "0"
    assert (box.work / "ran").exists()


def test_queue_with_a_free_slot_starts_normally(bgjob, box):
    box.active.discard(f"bgjob-{box.other}")
    assert _run(bgjob, "run", "--quiet", "--queue", "--cmd", "echo hi") == 0
    d = _new_job(box)
    assert not (d / "QUEUED").exists()
    assert "wait-slot" not in (d / "run.sh").read_text()


def test_queue_is_refused_for_a_prompt_job(bgjob, box):
    rc = _run(bgjob, "run", "--queue", "do research")
    assert "--queue is for --cmd jobs only" in str(rc)
    assert [p.name for p in box.root.iterdir()] == [box.other]


def test_queued_job_shows_as_queued_in_list_and_pin(bgjob, box):
    assert _run(bgjob, "run", "--queue", "--name", "rel", "--cmd", "echo hi") == 0
    d = _new_job(box)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["queued"] is True
    assert bgjob._job_state(d.name, meta) == "RUNNING (queued for a slot)"
    pin = bgjob.render_jobs_status()
    (line,) = [ln for ln in pin.splitlines() if "rel" in ln]
    assert "queued" in line, pin
