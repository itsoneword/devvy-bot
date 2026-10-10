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


def _write_run_sh(bgjob, tmp_path, monkeypatch) -> Path:
    """Have cmd_run write a run.sh (no systemd) and return its path."""
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": "t",
            "allow_dir": None,
            "model": "sonnet",
        },
    )()
    bgjob.cmd_run(args)
    return next(tmp_path.glob("*/run.sh"))


def _run_tail_block(run_sh: Path, output_bytes: bytes) -> bytes:
    """Write output.md, then execute run.sh's own finish-tail block (from
    `out_bytes=` to its closing `fi`) exactly as emitted, and return $body."""
    import subprocess

    (run_sh.parent / "output.md").write_bytes(output_bytes)
    lines = run_sh.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("out_bytes="))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    block = "\n".join(lines[start : end + 1])
    script = block + "\nprintf '%s' \"$body\"\n"
    # Popen, not run: the fixture monkeypatches subprocess.run (same module).
    proc = subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    return out


class TestOutputTailIsCharacterSafe:
    """dv-c9d1: `tail -c 1500` is a byte cut; starting mid-character sent
    lone surrogates down to the notifier and the ✅ ping was lost."""

    def test_tail_starting_mid_em_dash_is_valid_utf8_and_capped(
        self, bgjob, tmp_path, monkeypatch
    ):
        run_sh = _write_run_sh(bgjob, tmp_path, monkeypatch)
        # e2 80 94 = "—". With 1498 trailing bytes, the last 1500 bytes
        # start at 80 94 -- the em dash's lead byte is cut off.
        content = ("x" * 100 + "—" + "a" * 1498).encode("utf-8")
        assert content[-1500:][:2] == b"\x80\x94"

        out = _run_tail_block(run_sh, content)

        text = out.decode("utf-8")  # strict: must not raise
        assert text.startswith("…")  # truncation is still marked
        tail = text[len("…") :]
        assert len(tail.encode("utf-8")) <= 1500
        assert tail.endswith("a" * 1498)
        assert "�" not in tail

    def test_short_multibyte_output_passes_through_whole(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: under the cap, valid UTF-8 (emoji, em dash) is untouched and
        gets no ellipsis -- the filter must not eat valid characters."""
        run_sh = _write_run_sh(bgjob, tmp_path, monkeypatch)
        content = "✅ done — 😀 Как плечо?\nline two"

        out = _run_tail_block(run_sh, content.encode("utf-8"))

        assert out.decode("utf-8") == content
