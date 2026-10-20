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


class TestProfileResolution:
    """Jobs resolve profiles through the bot's registry loader: a private YAML
    parser left ${DEVVY_WORKSPACES} unexpanded, so every job for a
    template-created profile failed, and extra_dirs never reached the job."""

    def test_extra_dirs_become_writable_binds(self, bgjob, tmp_path, monkeypatch):
        extra = tmp_path / "project"
        extra.mkdir()
        monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [str(extra)])
        run_sh = _write_run_sh(bgjob, tmp_path, monkeypatch).read_text()
        assert f"--bind {extra} {extra}" in run_sh

    def test_registry_dump_resolves_workspace_variable(self, bgjob):
        """The real loader, on the real config: no unexpanded ${...} left."""
        if not bgjob.PROFILES_YAML.exists():
            pytest.skip("no config/profiles.yaml in this checkout")
        dirs = bgjob.load_profiles()
        assert dirs, "no profiles resolved"
        assert all("${" not in d and d.startswith("/") for d in dirs.values())


class TestSummary:
    """dv-dfb1: <summary> extraction with a tail fallback."""

    def test_last_summary_block_wins(self, bgjob):
        text = "x\n<summary>old</summary>\nmore\n<summary>\nA\nB\n</summary>\n"
        assert bgjob.extract_summary(text) == "A\nB"

    def test_summary_capped_to_15_lines(self, bgjob):
        body = "\n".join(f"l{i}" for i in range(40))
        out = bgjob.extract_summary(f"<summary>{body}</summary>")
        assert out.splitlines() == [f"l{i}" for i in range(15)]

    def test_fallback_is_last_15_lines(self, bgjob):
        text = "\n".join(f"l{i}" for i in range(40))
        out = bgjob.extract_summary(text).splitlines()
        assert out[0].startswith("(no summary block")
        assert out[1:] == [f"l{i}" for i in range(25, 40)]

    def test_write_summary_empty_output(self, bgjob, tmp_path, monkeypatch):
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        (tmp_path / "j").mkdir()
        (tmp_path / "j" / "output.md").write_text("")
        assert bgjob.write_summary("j").startswith("(no output)")
        assert (tmp_path / "j" / "summary.md").exists()

    def test_output_default_is_summary_full_is_everything(
        self, bgjob, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        d = tmp_path / "j"
        d.mkdir()
        (d / "output.md").write_text("long\nstuff\n<summary>short</summary>\n")
        (d / "exit_code").write_text("0")
        ns = type("A", (), {"id": "j", "tail": 0, "full": False})()
        bgjob.cmd_output(ns)
        out = capsys.readouterr().out
        assert "short" in out and "long" not in out
        ns.full = True
        bgjob.cmd_output(ns)
        assert "long" in capsys.readouterr().out


class TestListLimit:
    def test_running_first_then_newest_n_finished(
        self, bgjob, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        for i in range(12):
            d = tmp_path / f"2026010{i % 10}-1200{i:02d}-j{i}"
            d.mkdir(exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"unit": f"u{i}"}))
            (d / "exit_code").write_text("0")
        monkeypatch.setattr(bgjob, "unit_active", lambda u: u == "u0")
        monkeypatch.setattr(bgjob, "job_progress", lambda u: "")
        ns = type("A", (), {"limit": 3, "compact": True})()
        bgjob.cmd_list(ns)
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1 + 3 + 1  # running + 3 finished + footer
        assert "RUNNING" in lines[0]
        assert lines[-1].startswith("+") and "all=true" in lines[-1]


def test_run_prepends_preamble_and_ticket(bgjob, tmp_path, monkeypatch):
    monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

    class R:
        returncode = 0
        stderr = ""

        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(
        bgjob.subprocess,
        "run",
        lambda cmd, **kw: R("/x/dv-aaaa.md\n" if "path" in cmd else "{}"),
    )
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": "t",
            "allow_dir": None,
            "model": "sonnet",
            "task": "dv-aaaa",
        },
    )()
    bgjob.cmd_run(args)
    prompt = next(tmp_path.glob("*/prompt.txt")).read_text()
    assert "<summary>" in prompt
    assert "Ticket: /x/dv-aaaa.md" in prompt
    assert prompt.rstrip().endswith("do it")
    assert "summarize" in next(tmp_path.glob("*/run.sh")).read_text()


class TestCmdJobs:
    def _args(self, **kw):
        base = {
            "prompt": None,
            "cmd": "echo hi",
            "profile": "general",
            "name": "full-tests",
            "allow_dir": None,
            "model": "sonnet",
        }
        base.update(kw)
        return type("A", (), base)()

    def _setup(self, bgjob, tmp_path, monkeypatch):
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
        monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
        calls = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            bgjob.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R()
        )
        monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)
        return calls

    def test_run_sh_runs_command_without_model_and_pings_with_job_ref(
        self, bgjob, tmp_path, monkeypatch
    ):
        calls = self._setup(bgjob, tmp_path, monkeypatch)
        assert bgjob.cmd_run(self._args()) == 0
        run_sh = next(tmp_path.glob("*/run.sh"))
        text = run_sh.read_text()
        job_id = run_sh.parent.name
        assert "agentjob" not in text and "bwrap" not in text
        assert "cmd.txt" in text
        assert f"--ref job:{job_id}" in text
        assert "--profile general" in text
        assert (run_sh.parent / "cmd.txt").read_text() == "echo hi"
        assert any(c[:2] == ["systemd-run", "--user"] for c in calls)

    def test_cmd_rejects_prompt_and_missing_prompt(self, bgjob, tmp_path, monkeypatch):
        self._setup(bgjob, tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            bgjob.cmd_run(self._args(prompt="x"))
        with pytest.raises(SystemExit):
            bgjob.cmd_run(self._args(cmd=None))

    def test_run_sh_executes_and_summarizes(self, bgjob, tmp_path, monkeypatch):
        import subprocess

        self._setup(bgjob, tmp_path, monkeypatch)
        bgjob.cmd_run(self._args(cmd="echo '= 3 passed in 1s ='; exit 2", quiet=True))
        d = next(tmp_path.glob("*/run.sh")).parent
        # Run the real script (summarize step uses this bgjob file + sys.executable).
        monkeypatch.undo()
        monkeypatch.setenv("BGJOB_ROOT", str(tmp_path))
        proc = subprocess.Popen(["bash", str(d / "run.sh")])
        proc.communicate(timeout=60)
        assert (d / "exit_code").read_text().strip() == "2"
        summary = (d / "summary.md").read_text()
        assert summary.startswith("exit code: 2")
        assert "3 passed" in summary

    def test_oneline_prefers_pytest_line(self, bgjob):
        text = "...\n=== 2 failed, 10 passed in 5.1s ===\n"
        assert bgjob.cmd_oneline_text(text) == "2 failed, 10 passed in 5.1s"
        assert bgjob.cmd_oneline_text("a\nlast one\n") == "last one"
        assert bgjob.cmd_oneline_text("") == "(no output)"
