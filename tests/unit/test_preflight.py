"""Preflight dependency check (dv-7d92 step 1).

What matters here is that a missing dependency is *reported*, with a fix, and
sets a non-zero exit code -- the installer's decision to stop or to offer an
auto-install is driven entirely by this. A preflight that quietly passes on a
box without `bwrap` or without systemd hands the user a bot that fails hours
later, in a journal entry.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "preflight", REPO_ROOT / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], and blows up with AttributeError without this.
sys.modules["preflight"] = preflight
_spec.loader.exec_module(preflight)


def _check(**kw):
    base = dict(
        key="x",
        label="Thing",
        why="because",
        fix="apt-get install thing",
        required=True,
    )
    base.update(kw)
    return preflight.Check(**base)


# --- version parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "banner,expected",
    [
        ("v22.22.2", (22, 22, 2)),
        ("Poetry (version 2.3.2)", (2, 3, 2)),
        ("2.1.257 (Claude Code)", (2, 1, 257)),
        ("systemd 255 (255.4-1ubuntu8.17)", (255,)),
        ("git version 2.43.0", (2, 43, 0)),
        ("no numbers here", ()),
        ("", ()),
    ],
)
def test_parse_version_handles_every_banner_we_actually_meet(banner, expected):
    assert preflight.parse_version(banner) == expected


@pytest.mark.parametrize(
    "found,minimum,ok",
    [
        ((3, 12, 3), (3, 11), True),
        ((3, 11, 0), (3, 11), True),
        ((3, 10, 14), (3, 11), False),
        ((4, 0), (3, 11), True),
        ((20,), (18,), True),
        ((16, 20, 2), (18,), False),
        ((), (3, 11), False),
    ],
)
def test_at_least_compares_only_the_components_the_minimum_names(found, minimum, ok):
    assert preflight.at_least(found, minimum) is ok


# --- probes -------------------------------------------------------------


def test_run_version_returns_none_for_a_missing_binary():
    assert preflight.run_version(["definitely-not-a-real-binary-xyz"]) is None


def test_run_version_reads_stderr_banners_too():
    out = preflight.run_version(
        ["python3", "-c", "import sys; print('v1.2', file=sys.stderr)"]
    )
    assert out == "v1.2"


def test_python_check_uses_the_running_interpreter():
    result = preflight.check_python(_check(key="python"))
    assert result.ok is True  # the test suite itself requires >= 3.11
    assert result.found.startswith("3.")


def test_non_linux_fails_the_os_check(monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "arm64")

    result = preflight.check_os(_check(key="os"))

    assert result.ok is False
    assert "Darwin" in result.found


def test_systemctl_without_systemd_running_is_not_enough(monkeypatch):
    """A container ships the client and nothing to talk to; `systemctl enable`
    would be the first thing to notice, far too late."""
    monkeypatch.setattr(preflight, "run_version", lambda cmd: "systemd 255")
    monkeypatch.setattr(preflight.os.path, "exists", lambda p: False)

    result = preflight.check_systemd(_check(key="systemd"))

    assert result.ok is False
    assert "not the init system" in result.note


def test_missing_bwrap_is_optional_not_fatal(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    check = _check(key="bwrap", required=False)
    result = preflight.check_bwrap(check)

    assert result.ok is False
    assert preflight.missing_required([result]) == []


# --- reporting ----------------------------------------------------------


def test_missing_required_drives_the_exit_code():
    good = preflight.Result(_check(), True, "1.0")
    bad_required = preflight.Result(_check(key="a", label="A"), False)
    bad_optional = preflight.Result(_check(key="b", label="B", required=False), False)

    assert preflight.missing_required([good, bad_optional]) == []
    assert preflight.missing_required([good, bad_required, bad_optional]) == [
        bad_required
    ]


def test_table_marks_and_fix_commands():
    results = [
        preflight.Result(_check(key="a", label="Present"), True, "1.0"),
        preflight.Result(
            _check(key="b", label="Absent", fix="apt-get install absent"), False, ""
        ),
        preflight.Result(
            _check(
                key="c", label="Optional", required=False, fix="apt-get install opt"
            ),
            False,
        ),
    ]

    table = preflight.render_table(results)

    assert f"{preflight.TICK}  Present" in table
    assert f"{preflight.CROSS}  Absent" in table
    assert f"{preflight.WARN}  Optional" in table
    assert "fix: apt-get install absent" in table
    assert "fix: apt-get install opt" in table


def test_json_payload_carries_what_install_sh_reads():
    result = preflight.Result(
        _check(key="poetry", label="Poetry", autofix="curl ... | python3 -"), False
    )

    payload = result.as_dict()

    assert payload["key"] == "poetry"
    assert payload["ok"] is False
    assert payload["autofix"] == "curl ... | python3 -"
    assert payload["fix"]


def test_a_passing_check_offers_no_autofix():
    """install.sh iterates on `not ok and autofix`; a stale command offered for
    an installed dependency would re-run apt for no reason."""
    result = preflight.Result(_check(autofix="apt-get install thing"), True, "1.0")
    assert result.as_dict()["autofix"] == ""
    assert result.as_dict()["fix"] == ""


def test_every_shipped_check_has_a_fix_command():
    for spec in preflight.build_specs():
        assert spec.check.fix.strip(), spec.check.key
        assert spec.check.why.strip(), spec.check.key


def test_full_run_on_this_machine_reports_every_dependency():
    results = preflight.run_checks()
    keys = {r.check.key for r in results}
    assert {
        "os",
        "systemd",
        "python",
        "git",
        "poetry",
        "node",
        "claude",
        "bwrap",
    } <= keys
