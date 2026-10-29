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
        # The batch digest check runs only after the ✅ ping got through
        # (`bgjob digest`, DECISIONS 2026-10-22 "Jobs is a log").
        assert f"digest {job_id}" in run_sh
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


def _ping_header(run_sh: Path, rc: int) -> str:
    """Execute run.sh's own ping-header block (from `d=$((` up to `hdr=`) as
    emitted, with the given exit code, and return the built $msg."""
    import subprocess

    lines = run_sh.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("d=$(("))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("hdr="))
    block = "\n".join(lines[start:end])
    script = f"rc={rc}\nt0=$(date +%s)\nstarted_h=now\n{block}\nprintf '%s' \"$msg\"\n"
    # Popen, not run: the fixture monkeypatches subprocess.run (same module).
    proc = subprocess.Popen(
        ["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    return out.decode("utf-8")


class TestIncompletePing:
    """dv-b6dd: agentjob exits 3 when the job's own background work was still
    running at its end; the ping must say so instead of ✅ finished."""

    def test_clean_exit_still_pings_finished(self, bgjob, tmp_path, monkeypatch):
        """Scenario 1 (guard): rc 0 -> ✅ finished, as today."""
        msg = _ping_header(_write_run_sh(bgjob, tmp_path, monkeypatch), 0)
        first = msg.splitlines()[0]
        assert first.startswith("✅") and "finished" in first
        assert "INCOMPLETE" not in msg

    def test_exit_3_pings_incomplete_not_ok(self, bgjob, tmp_path, monkeypatch):
        """Scenario 3: rc 3 -> ⚠️ INCOMPLETE, never ✅ / finished."""
        msg = _ping_header(_write_run_sh(bgjob, tmp_path, monkeypatch), 3)
        first = msg.splitlines()[0]
        assert first.startswith("⚠️") and "INCOMPLETE" in first
        assert "✅" not in msg and "❌" not in first

    def test_other_failures_stay_failed(self, bgjob, tmp_path, monkeypatch):
        """Other non-zero exits keep the ❌ FAILED ping."""
        run_sh = _write_run_sh(bgjob, tmp_path, monkeypatch)
        for rc in (1, 2, 137):
            first = _ping_header(run_sh, rc).splitlines()[0]
            assert first.startswith("❌") and "FAILED" in first, rc
            assert "INCOMPLETE" not in first


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

    def test_image_lines_survive_cap(self, bgjob):
        body = "\n".join(f"l{i}" for i in range(30))
        body += "\nimage: /w/a.png\nIMAGE: /w/b.png"
        out = bgjob.extract_summary(f"<summary>{body}</summary>").splitlines()
        assert out[:15] == [f"l{i}" for i in range(15)]
        assert out[15:] == ["image: /w/a.png", "IMAGE: /w/b.png"]

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


# --------------------------------------------------------------------------
# dv-5e2f: agent jobs must not reach the user systemd bus from inside bwrap
# (a `systemd-run --user` service runs outside the box with / writable).
# --------------------------------------------------------------------------

import os as _os  # noqa: E402
import re as _re  # noqa: E402
import shlex as _shlex  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import sys as _sys  # noqa: E402

_UID = _os.getuid()
_RUN_USER = f"/run/user/{_UID}"
_BIND_OPTS = {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try", "--dev-bind"}
_SIZE = {"": 1, "B": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}


def _bytes(size: str) -> int:
    m = _re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)B?", size.strip())
    assert m, f"not a systemd size: {size!r}"
    return int(float(m.group(1)) * _SIZE[m.group(2)])


def _agent_job(bgjob, tmp_path, monkeypatch, python_bin=None, name="t"):
    """cmd_run for an agent (prompt) job with the REAL bwrap path, nothing
    launched. Returns (run.sh path, systemd-run argv, python_bin)."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    python_bin = python_bin or _sys.executable
    real_bwrap = _shutil.which("bwrap") or "/usr/bin/bwrap"
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: python_bin)
    monkeypatch.setattr(
        bgjob.shutil, "which", lambda n: real_bwrap if n == "bwrap" else "/bin/sh"
    )
    calls = []

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(
        bgjob.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or R()
    )
    args = type(
        "A",
        (),
        {
            "prompt": "do it",
            "profile": "general",
            "name": name,
            "allow_dir": None,
            "model": "sonnet",
            "quiet": True,
        },
    )()
    assert bgjob.cmd_run(args) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    units = [c for c, _ in calls if c and c[0] == "systemd-run"]
    assert units, calls
    return run_sh, units[-1], python_bin


def _box_prefix(run_sh, python_bin) -> str:
    """The bwrap part of run.sh's agentjob line: everything before the python
    that runs scripts/agentjob."""
    head = _shlex.quote(python_bin) + " "
    for line in run_sh.read_text().splitlines():
        if "agentjob" in line and head in line and "bwrap" in line:
            return line[: line.index(head)]
    raise AssertionError("no bwrap ... agentjob line in run.sh")


def _run_in_box(prefix: str, probe: str, timeout: int = 60):
    # Popen: the fixtures may have patched subprocess.run module-wide.
    script = f"box_cg=''\n{prefix} /bin/sh -c {_shlex.quote(probe)}\n"
    proc = _subprocess.Popen(
        ["bash", "-c", script],
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


def _ok(argv: list) -> bool:
    try:
        r = _subprocess.run(argv, capture_output=True, timeout=30)
    except (OSError, _subprocess.SubprocessError):
        return False
    return r.returncode == 0


_HAVE_BWRAP = bool(_shutil.which("bwrap")) and _ok(
    ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--die-with-parent", "true"]
)
# The job box binds ~/.claude; a clean checkout (CI, a contributor) has none.
_HAVE_BWRAP = _HAVE_BWRAP and (Path.home() / ".claude").is_dir()
_HAVE_BUS = _os.path.exists(f"{_RUN_USER}/bus")
_SERVICE_WORKS = (
    _HAVE_BUS
    and bool(_shutil.which("systemd-run"))
    and _ok(["systemd-run", "--user", "--wait", "--quiet", "--collect", "true"])
)


class TestJobBoxHidesUserBus:
    def test_agent_job_systemd_run_service_fails_and_starts_nothing(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario 1 (live): inside the real job box,
        `systemd-run --user --wait --pipe true` fails and no unit runs."""
        if not (_HAVE_BWRAP and _SERVICE_WORKS):
            pytest.skip("needs bwrap and a user systemd that starts services")
        run_sh, _, py = _agent_job(bgjob, tmp_path, monkeypatch)
        prefix = _box_prefix(run_sh, py)
        monkeypatch.undo()
        marker = tmp_path / "escaped"
        unit = f"dv5e2f-probe-{_os.getpid()}"
        probe = (
            "systemd-run --user --wait --pipe true; echo rc1=$?; "
            f"systemd-run --user --wait --quiet --collect --unit={unit} "
            f"-- /bin/sh -c 'echo started > {marker}'; echo rc2=$?"
        )
        rc, out, err = _run_in_box(prefix, probe)
        assert rc == 0, err
        assert "rc1=0" not in out, (out, err)
        assert "rc2=0" not in out, (out, err)
        assert not marker.exists(), "a transient service ran outside the box"

    def test_live_probe_real_box_prefix_cannot_reach_user_bus(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario 5 (the ticket's regression probe): with the real box
        prefix, the bus socket, systemd's private bus and the bus address are
        all gone."""
        if not (_HAVE_BWRAP and _HAVE_BUS):
            pytest.skip("needs bwrap and a user bus on this host")
        run_sh, _, py = _agent_job(bgjob, tmp_path, monkeypatch)
        prefix = _box_prefix(run_sh, py)
        monkeypatch.undo()
        probe = (
            f"test -S {_RUN_USER}/bus && echo BUS; "
            f"test -e {_RUN_USER}/systemd/private && echo PRIVATE; "
            'test -n "$DBUS_SESSION_BUS_ADDRESS" && echo ADDR; '
            'test -n "$XDG_RUNTIME_DIR" && test -S "$XDG_RUNTIME_DIR/bus" '
            "&& echo XDGBUS; echo done"
        )
        rc, out, err = _run_in_box(prefix, probe)
        assert rc == 0, err
        assert out.split() == ["done"], out

    def test_box_prefix_masks_run_user_and_unsets_bus_address(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added (no bwrap needed): the box argv hides /run/user with a tmpfs
        after `--ro-bind / /`, unsets DBUS_SESSION_BUS_ADDRESS, and no later
        bind re-exposes anything under /run/user."""
        run_sh, _, py = _agent_job(bgjob, tmp_path, monkeypatch)
        toks = [t for t in _shlex.split(_box_prefix(run_sh, py)) if "$" not in t]
        assert toks[0].endswith("bwrap"), toks
        ro = next(
            i
            for i in range(len(toks) - 2)
            if toks[i : i + 3] == ["--ro-bind", "/", "/"]
        )
        masks = [
            i
            for i in range(len(toks) - 1)
            if toks[i] == "--tmpfs"
            and toks[i + 1].rstrip("/") in {"/run", "/run/user", _RUN_USER}
        ]
        assert masks and masks[0] > ro, toks
        assert any(
            toks[i : i + 2] == ["--unsetenv", "DBUS_SESSION_BUS_ADDRESS"]
            for i in range(len(toks) - 1)
        ), toks
        mask = masks[0]
        for i in range(mask + 2, len(toks) - 2):
            if toks[i] in _BIND_OPTS:
                for p in toks[i + 1 : i + 3]:
                    assert not p.startswith("/run/user"), toks[i : i + 3]

    def test_agent_job_unit_delegates_cgroup_with_room_for_tests(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: tests now live inside the job cgroup, so the agent unit gets
        Delegate=yes, MemoryMax = job cap + test cap, and run.sh carves
        box/agent and binds box/ rw into the box."""
        monkeypatch.setattr(bgjob, "JOB_MEMORY_MAX", "1G")
        monkeypatch.setattr(bgjob, "TEST_MEMORY_MAX", "900M")
        monkeypatch.setenv("BGJOB_MEMORY_MAX", "1G")
        monkeypatch.setenv("PUMP_TEST_MEMORY_MAX", "900M")
        run_sh, argv, py = _agent_job(bgjob, tmp_path, monkeypatch)
        assert any(a in ("--property=Delegate=yes", "Delegate=yes") for a in argv)
        caps = [
            m.group(1)
            for a in argv
            if (m := _re.fullmatch(r"(?:--property=)?MemoryMax=(\S+)", a))
        ]
        assert caps and _bytes(caps[-1]) == _bytes("1G") + _bytes("900M"), argv
        text = run_sh.read_text()
        assert "box/agent" in text and "cgroup.procs" in text, text
        assert "subtree_control" in text, text

    def test_pump_job_testrun_oom_is_reported_and_agent_survives(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario 2 (live): the real run.sh in the real unit argv; the
        'agent' runs scripts/testrun on a 200M allocation under a 64M cap.
        testrun prints its OOM line, the agent goes on and the job ends rc 0."""
        if not (_HAVE_BWRAP and _SERVICE_WORKS):
            pytest.skip("needs bwrap and a user systemd that starts services")
        testrun = _BGJOB_PATH.parent / "testrun"
        hog = "b = bytearray(200 * 2**20); b[::4096] = b'x' * len(b[::4096])"
        stub = tmp_path / "fakepython"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  */agentjob)\n"
            f"    {_shlex.quote(str(testrun))} --mem 64M -- "
            f"{_shlex.quote(_sys.executable)} -c {_shlex.quote(hog)}\n"
            '    echo "agent survived rc=$?"\n'
            "    exit 0 ;;\n"
            f'  *) exec {_shlex.quote(_sys.executable)} "$@" ;;\n'
            "esac\n"
        )
        stub.chmod(0o755)
        run_sh, argv, _ = _agent_job(
            bgjob, tmp_path, monkeypatch, python_bin=str(stub), name="dv5e2f-live"
        )
        env = bgjob._env()
        monkeypatch.undo()
        live = [argv[0], argv[1], "--wait", *argv[2:]]
        proc = _subprocess.Popen(
            live, env=env, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True
        )
        _, err = proc.communicate(timeout=180)
        d = run_sh.parent
        assert proc.returncode == 0, err
        assert (d / "exit_code").read_text().strip() == "0"
        out = (d / "output.md").read_text()
        assert "agent survived" in out, out
        log = (d / "stderr.log").read_text()
        assert "testrun: OOM-killed at 64M" in log, log


class TestCmdJobsKeepTheBus:
    def test_cmd_job_has_no_box_no_delegate_and_keeps_bus(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario 4: --cmd jobs are unchanged: no bwrap, no Delegate, the
        job cap alone, and nothing hides the user bus."""
        monkeypatch.setattr(bgjob, "JOB_MEMORY_MAX", "1G")
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
        monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
        calls = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            bgjob.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or R()
        )
        monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)
        args = type(
            "A",
            (),
            {
                "prompt": None,
                "cmd": "systemd-run --user --scope true",
                "profile": "general",
                "name": "full-tests",
                "allow_dir": None,
                "model": "sonnet",
                "quiet": True,
            },
        )()
        assert bgjob.cmd_run(args) == 0
        text = next(tmp_path.glob("*/run.sh")).read_text()
        for word in ("bwrap", "DBUS_SESSION_BUS_ADDRESS", "--tmpfs", "cgroup"):
            assert word not in text, word
        unit = [(c, kw) for c, kw in calls if c and c[0] == "systemd-run"]
        assert len(unit) == 1, calls
        argv, kw = unit[0]
        assert not any("Delegate" in a for a in argv), argv
        caps = [a for a in argv if "MemoryMax=" in a]
        assert caps == ["--property=MemoryMax=1G"], argv
        assert kw.get("env", {}).get("XDG_RUNTIME_DIR"), kw


# --------------------------------------------------------------------------
# dv-5432 phase 2: command jobs queue for a job slot; prompt jobs are refused.
# `bgjob run --queue`: with the cap full the unit still starts, writes QUEUED
# in its job dir, and run.sh runs `bgjob wait-slot <id>` before the command.
# live_jobs() does not count a QUEUED job. wait-slot polls (time.sleep) until
# live non-queued jobs other than itself < cap, then removes QUEUED; gives up
# after 2 h with rc 75. Design: docs/research/dv-5432/analysis.md decision 6.
# --------------------------------------------------------------------------


class _StillWaiting(BaseException):
    """Raised by the fake sleep to stop a waiter the test expects to block."""


class _QueueBox:
    """A jobs root with fake units: `active` is the set of running unit names.

    systemd-run marks its unit active, as the real one would; every other
    subprocess call (agentctl notify, pin) is recorded and succeeds.
    """

    def __init__(self, bgjob, tmp_path, monkeypatch, cap):
        self.bgjob = bgjob
        self.root = tmp_path / "jobs"
        self.root.mkdir()
        self.active: set = set()
        self.calls: list = []
        self.sleeps = 0
        self.on_sleep = None
        self.clock = 1_000_000.0
        monkeypatch.setattr(bgjob, "JOBS_ROOT", self.root)
        monkeypatch.setattr(bgjob, "MAX_CONCURRENT", cap)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
        monkeypatch.setattr(bgjob, "unit_active", lambda unit: unit in self.active)
        monkeypatch.setattr(bgjob, "job_progress", lambda unit: "")
        monkeypatch.setattr(bgjob.shutil, "which", lambda name: "/bin/sh")

        box = self

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run(cmd, **kw):
            box.calls.append(list(cmd))
            if cmd and cmd[0] == "systemd-run":
                unit = next(a for a in cmd if a.startswith("--unit="))
                box.active.add(unit.split("=", 1)[1])
            return R()

        monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
        monkeypatch.setattr(bgjob.subprocess, "Popen", lambda *a, **k: None)

        import time as real_time

        class FakeTime:
            """Fake clock: sleep advances it and lets the test free a slot."""

            def __getattr__(self, name):
                return getattr(real_time, name)

            def time(self):
                return box.clock

            def monotonic(self):
                return box.clock

            def sleep(self, secs):
                box.sleeps += 1
                box.clock += max(float(secs), 1.0)
                if box.sleeps > 2000:
                    raise AssertionError("wait-slot never returned")
                if box.on_sleep:
                    box.on_sleep(box.sleeps)

        monkeypatch.setattr(bgjob, "time", FakeTime())

    def running_job(self, name, queued=False):
        jid = f"20261025-1200{len(self.active):02d}-{name}"
        d = self.root / jid
        d.mkdir()
        unit = f"bgjob-{jid}"
        (d / "meta.json").write_text(
            json.dumps({"id": jid, "name": name, "profile": "general", "unit": unit})
        )
        if queued:
            (d / "QUEUED").write_text("")
        self.active.add(unit)
        return jid

    def stop(self, jid):
        self.active.discard(f"bgjob-{jid}")

    def main(self, *argv):
        """bgjob's real CLI entry point; returns the exit code."""
        import sys as sys_mod

        old = sys_mod.argv
        sys_mod.argv = ["bgjob", *argv]
        try:
            rc = self.bgjob.main()
        except SystemExit as e:
            rc = e.code
        finally:
            sys_mod.argv = old
        return rc

    def systemd_runs(self):
        return [c for c in self.calls if c and c[0] == "systemd-run"]

    def new_job_dirs(self, before):
        return sorted(p for p in self.root.iterdir() if p.name not in before)


class TestCommandJobsQueueForASlot:
    def test_command_job_with_cap_full_is_queued_and_starts_when_slot_frees(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario: job_start(command) with the job cap full is queued (not
        refused) and starts once a slot frees."""
        box = _QueueBox(bgjob, tmp_path, monkeypatch, cap=1)
        other = box.running_job("research")
        before = {p.name for p in box.root.iterdir()}

        rc = box.main(
            "run", "--profile", "general", "--name", "rel", "--queue",
            "--cmd", "echo release",
        )  # fmt: skip

        assert rc == 0
        (d,) = box.new_job_dirs(before)
        jid = d.name
        assert len(box.systemd_runs()) == 1, "queued job's unit must still start"
        assert (d / "QUEUED").exists()
        # A queued job does not take the slot it is waiting for.
        assert bgjob.live_jobs() == [other]
        # run.sh waits for the slot before it runs the command.
        run_sh = (d / "run.sh").read_text()
        wait_at = run_sh.find(f"wait-slot {jid}")
        assert wait_at != -1, run_sh
        assert wait_at < run_sh.find("cmd.txt"), run_sh
        # The start ping says it is queued, and why.
        pings = " ".join(" ".join(c) for c in box.calls if "notify" in c)
        assert "queued" in pings and "cap 1" in pings, pings

        # The other job finishes while the queued one polls.
        box.on_sleep = lambda n: box.stop(other)
        assert box.main("wait-slot", jid) == 0
        assert box.sleeps >= 1
        assert not (d / "QUEUED").exists()
        assert bgjob.live_jobs() == [jid]

    def test_wait_slot_returns_at_once_when_a_slot_is_free(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: a queued job whose slot is already free does not sit out a
        poll interval (its own active unit does not count against it)."""
        box = _QueueBox(bgjob, tmp_path, monkeypatch, cap=1)
        jid = box.running_job("rel", queued=True)

        assert box.main("wait-slot", jid) == 0
        assert box.sleeps == 0
        assert not (box.root / jid / "QUEUED").exists()

    def test_second_waiter_does_not_take_a_slot_the_first_one_took(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: two queued jobs and one free slot: only one passes."""
        box = _QueueBox(bgjob, tmp_path, monkeypatch, cap=2)
        box.running_job("research")
        first = box.running_job("rel-a", queued=True)
        second = box.running_job("rel-b", queued=True)

        assert box.main("wait-slot", first) == 0

        def give_up(n):
            if n >= 3:
                raise _StillWaiting

        box.on_sleep = give_up
        with pytest.raises(_StillWaiting):
            box.main("wait-slot", second)
        assert (box.root / second / "QUEUED").exists()

    def test_wait_slot_gives_up_after_two_hours_with_rc_75(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: the cap never frees: rc 75 after 2 h, not an endless wait."""
        box = _QueueBox(bgjob, tmp_path, monkeypatch, cap=1)
        box.running_job("research")
        jid = box.running_job("rel", queued=True)
        start = box.clock

        assert box.main("wait-slot", jid) == 75
        waited = box.clock - start
        assert 2 * 3600 <= waited <= 2 * 3600 + 120, waited


class TestPromptJobsStillRefusedAtCap:
    def test_prompt_job_with_cap_full_is_refused_and_starts_nothing(
        self, bgjob, tmp_path, monkeypatch
    ):
        """Scenario: prompt jobs with the cap full are still refused, as today
        (and a command job without --queue is too: the shell default holds)."""
        box = _QueueBox(bgjob, tmp_path, monkeypatch, cap=1)
        box.running_job("research")
        before = {p.name for p in box.root.iterdir()}

        for argv in (
            ("run", "--profile", "general", "--name", "p", "do it"),
            ("run", "--profile", "general", "--name", "c", "--cmd", "echo hi"),
        ):
            rc = box.main(*argv)
            assert rc not in (0, None), argv
            assert "already running" in str(rc) and "cap 1" in str(rc), rc
        assert box.systemd_runs() == []
        assert box.new_job_dirs(before) == []


# --------------------------------------------------------------------------
# dv-d63d phase 2: jobs resolve their model from the route table.
#
# Contract (docs/research/dv-d63d/analysis.md, Phase 2):
# * `bgjob run --route NAME` hands the job route name to agentjob, which
#   resolves harness/provider/model from models.yaml `jobs:`;
# * `--model` has no default any more: without it run.sh passes no
#   `--model`, so the table row decides (an always-sent `sonnet` would beat
#   every row, and on a non-Claude route it would be a Claude tier);
# * the legacy PUMP_*_MODEL env (taskpump's environment) must still reach
#   agentjob, which runs in a transient unit that does not inherit our env.
# --------------------------------------------------------------------------


def _main_run(bgjob, tmp_path, monkeypatch, *argv):
    """`bgjob run <argv>` through the real parser and cmd_run; nothing is
    launched. Returns (agentjob line of run.sh, systemd-run argv, run.sh)."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    real_bwrap = _shutil.which("bwrap") or "/usr/bin/bwrap"
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: _sys.executable)
    monkeypatch.setattr(
        bgjob.shutil, "which", lambda n: real_bwrap if n == "bwrap" else "/bin/sh"
    )
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # Should bgjob ask agentjob for the resolved route, answer as the
        # route table would for a Claude row.
        out = (
            '{"harness": "claude", "provider": "anthropic_sub", "model": "sonnet"}'
            if "--print-route" in cmd
            else ""
        )
        return _subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(bgjob.subprocess, "run", fake_run)
    monkeypatch.setattr(
        _sys,
        "argv",
        ["bgjob", "run", "do it", "--profile", "general", "--quiet", *argv],
    )
    assert bgjob.main() == 0
    run_sh = next(jobs.glob("*/run.sh"))
    line = next(
        ln
        for ln in run_sh.read_text().splitlines()
        if "agentjob" in ln and "--prompt-file" in ln
    )
    units = [c for c in calls if c and c[0] == "systemd-run"]
    assert units, calls
    return line, units[-1], run_sh


class TestJobRoutes:
    def test_route_reaches_agentjob_and_no_model_is_forced(
        self, bgjob, tmp_path, monkeypatch
    ):
        """taskpump's `--route pump_review` gets to agentjob; with no
        --model the table row picks the model."""
        line, _unit, _ = _main_run(
            bgjob, tmp_path, monkeypatch, "--route", "pump_review"
        )
        assert "--route pump_review" in line
        assert "--model" not in line

    def test_plain_bgjob_without_model_sends_no_model(
        self, bgjob, tmp_path, monkeypatch
    ):
        """No models.yaml + no --model: agentjob's background_job row
        (sonnet, as today) decides, not a hardcoded default here."""
        line, _unit, _ = _main_run(bgjob, tmp_path, monkeypatch)
        assert "--model" not in line
        assert "--route" not in line or "--route background_job" in line

    def test_legacy_pump_model_env_reaches_the_job_unit(
        self, bgjob, tmp_path, monkeypatch
    ):
        """PUMP_REVIEW_MODEL set for taskpump must reach agentjob inside the
        transient unit (it does not inherit taskpump's environment)."""
        monkeypatch.setenv("PUMP_REVIEW_MODEL", "fable")
        line, unit, run_sh = _main_run(
            bgjob, tmp_path, monkeypatch, "--route", "pump_review"
        )
        assert "--model" not in line  # an explicit model would beat models.yaml
        assert "PUMP_REVIEW_MODEL=fable" in " ".join(unit) + run_sh.read_text()


# --------------------------------------------------------------------------
# dv-2514: an agent job's optional pre-agent command step (taskpump runs the
# board's acceptance command with it). `bgjob run <prompt> --pre-cmd <shell>
# --pre-log <file> --pre-timeout <s>`: run.sh runs the command inside the
# job's box, output to the log, BEFORE agentjob; `pre_exit_code` in the job
# dir. Past the timeout it is killed, `pre_timed_out` is written, agentjob
# never runs and `exit_code` is 124. `--cmd` + `--pre-cmd` is refused.
# --------------------------------------------------------------------------


def _pre_job(bgjob, tmp_path, monkeypatch, pre_cmd, pre_timeout):
    """cmd_run for an agent job with a pre-step, then run its run.sh for
    real with three seams: the box is a wrapper that sets BOXED=1, the
    cgroup carve-out is a no-op, and "python" is a fake that logs whether it
    ran agentjob. Returns (job dir, order log lines, pre-log path, rc)."""
    jobs, ws = tmp_path / "jobs", tmp_path / "ws"
    jobs.mkdir()
    ws.mkdir()
    order = tmp_path / "order.log"
    pre_log = ws / ".pump" / "acceptance" / "run.log"
    pre_log.parent.mkdir(parents=True)
    box = tmp_path / "box.sh"
    box.write_text('#!/bin/bash\nexport BOXED=1\nexec "$@"\n')
    box.chmod(0o755)
    fakepy = tmp_path / "fakepy"
    fakepy.write_text(
        "#!/bin/bash\n"
        'case "$*" in *agentjob*)\n'
        f"  echo agent >> {_shlex.quote(str(order))}\n"
        f"  [ -s {_shlex.quote(str(pre_log))} ] && "
        f"echo agent-saw-log >> {_shlex.quote(str(order))}\n"
        ";; esac\nexit 0\n"
    )
    fakepy.chmod(0o755)
    monkeypatch.setattr(bgjob, "JOBS_ROOT", jobs)
    monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(ws)})
    monkeypatch.setattr(bgjob, "load_extra_dirs", lambda slug: [])
    monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
    monkeypatch.setattr(bgjob, "_venv_python", lambda: str(fakepy))
    monkeypatch.setattr(bgjob, "job_box_prefix", lambda b, w: _shlex.quote(str(box)))
    monkeypatch.setattr(bgjob, "cgroup_carveout", lambda *a, **k: "box_cg=''\n")
    monkeypatch.setattr(bgjob.shutil, "which", lambda n: "/bin/sh")

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bgjob.subprocess, "run", lambda cmd, **kw: R())
    args = type(
        "A",
        (),
        {
            "prompt": "judge it",
            "profile": "general",
            "name": "pump-x-acceptance",
            "allow_dir": None,
            "model": "sonnet",
            "quiet": True,
            "cwd": str(ws),
            "pre_cmd": pre_cmd,
            "pre_log": str(pre_log),
            "pre_timeout": pre_timeout,
        },
    )()
    assert bgjob.cmd_run(args) == 0
    run_sh = next(jobs.glob("*/run.sh"))
    # Popen, not run: subprocess.run is patched module-wide above.
    proc = _subprocess.Popen(
        ["bash", str(run_sh)], stdout=_subprocess.PIPE, stderr=_subprocess.PIPE
    )
    proc.communicate(timeout=200)
    lines = order.read_text().splitlines() if order.exists() else []
    return run_sh.parent, lines, pre_log, proc.returncode


class TestPreAgentStep:
    def test_cli_takes_the_pre_step_flags(self, bgjob, monkeypatch):
        seen = {}
        monkeypatch.setattr(bgjob, "cmd_run", lambda a: seen.setdefault("a", a) and 0)
        monkeypatch.setattr(
            bgjob.sys,
            "argv",
            [
                "bgjob",
                "run",
                "judge it",
                "--pre-cmd",
                "scripts/acceptance --users 50",
                "--pre-log",
                "/w/.pump/acceptance/run.log",
                "--pre-timeout",
                "3600",
            ],
        )
        bgjob.main()
        a = seen["a"]
        assert a.pre_cmd == "scripts/acceptance --users 50"
        assert a.pre_log == "/w/.pump/acceptance/run.log"
        assert int(a.pre_timeout) == 3600

    def test_pre_step_runs_in_the_box_to_completion_before_the_agent(
        self, bgjob, tmp_path, monkeypatch
    ):
        order = tmp_path / "order.log"
        cmd = (
            "sleep 2; echo 'scenario 1 ok'; echo '{\"ok\": true}' > verdict.json; "
            f'echo "pre boxed=$BOXED" >> {_shlex.quote(str(order))}'
        )
        d, lines, pre_log, _ = _pre_job(bgjob, tmp_path, monkeypatch, cmd, 3600)
        # The step ran inside the job box, finished, then the agent started
        # and could already read the step's output.
        assert lines == ["pre boxed=1", "agent", "agent-saw-log"], lines
        assert "scenario 1 ok" in pre_log.read_text()
        assert (tmp_path / "ws" / "verdict.json").exists()  # cwd = worktree
        assert (d / "pre_exit_code").read_text().strip() == "0"
        assert not (d / "pre_timed_out").exists()
        assert (d / "exit_code").read_text().strip() == "0"
        assert (d / "pre_cmd.txt").read_text() == cmd

    def test_pre_step_past_its_timeout_is_killed_and_the_agent_never_runs(
        self, bgjob, tmp_path, monkeypatch
    ):
        order = tmp_path / "order.log"
        cmd = f"echo started; sleep 60; echo late >> {_shlex.quote(str(order))}"
        d, lines, pre_log, _ = _pre_job(bgjob, tmp_path, monkeypatch, cmd, 1)
        assert lines == [], lines  # no agent, the command never got past sleep
        assert (d / "pre_timed_out").exists()
        assert (d / "exit_code").read_text().strip() == "124"
        assert "started" in pre_log.read_text()

    def test_pre_step_failing_on_its_own_still_hands_over_to_the_agent(
        self, bgjob, tmp_path, monkeypatch
    ):
        """added: a non-zero exit (failing scenarios) is the agent's to judge;
        an exit 124 from the command itself, well inside the timeout, is not
        a hang."""
        for rc in (3, 124):
            sub = tmp_path / f"rc{rc}"
            sub.mkdir()
            d, lines, pre_log, _ = _pre_job(
                bgjob, sub, monkeypatch, f"echo 'scenario 2 FAIL'; exit {rc}", 3600
            )
            assert lines == ["agent", "agent-saw-log"], (rc, lines)
            assert (d / "pre_exit_code").read_text().strip() == str(rc)
            assert not (d / "pre_timed_out").exists(), rc
            assert (d / "exit_code").read_text().strip() == "0"

    def test_cmd_job_with_a_pre_step_is_refused(self, bgjob, tmp_path, monkeypatch):
        monkeypatch.setattr(bgjob, "JOBS_ROOT", tmp_path)
        monkeypatch.setattr(bgjob, "load_profiles", lambda: {"general": str(tmp_path)})
        monkeypatch.setattr(bgjob, "live_jobs", lambda: [])
        started = []

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(
            bgjob.subprocess, "run", lambda cmd, **kw: started.append(cmd) or R()
        )
        args = type(
            "A",
            (),
            {
                "prompt": None,
                "cmd": "pytest -q",
                "profile": "general",
                "name": "t",
                "allow_dir": None,
                "model": "sonnet",
                "pre_cmd": "echo hi",
                "pre_log": str(tmp_path / "x.log"),
                "pre_timeout": 60,
            },
        )()
        with pytest.raises(SystemExit):
            bgjob.cmd_run(args)
        assert not list(tmp_path.glob("*/run.sh"))
        assert not any(c and c[0] == "systemd-run" for c in started)
