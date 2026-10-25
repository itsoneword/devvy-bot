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
