"""dv-fe4d phase 1: the two macOS Seatbelt profiles (text only, any host).

job_profile mirrors scripts/bgjob's bwrap job box (reads everywhere, writes
only the job paths, no launchd/exec escape: dv-5e2f). bash_profile mirrors
src/security/bash_box.py (no reads under $HOME except the granted dirs).
"""

from __future__ import annotations

from pathlib import Path

from src.security import seatbelt
from src.security.bash_box import _HOME_RO

HOME = "/Users/tester"
ESCAPE_BINARIES = ("/bin/launchctl", "/usr/bin/open", "/usr/bin/osascript")


def _job(tmp_path: Path) -> tuple[str, Path, Path]:
    job_dir = tmp_path / "job"
    cwd = tmp_path / "repo"
    return seatbelt.job_profile([str(job_dir), str(cwd)]), job_dir, cwd


def _chat() -> str:
    return seatbelt.bash_profile(
        write_dirs=["/Users/tester/ws"],
        read_dirs=["/Users/tester/tools"],
        home=HOME,
    )


def test_profiles_deny_launchctl_open_osascript() -> None:
    for text in (_job(Path("/tmp/x"))[0], _chat()):
        assert "(version 1)" in text and "(deny default)" in text
        assert "(allow default)" not in text
        for exe in ESCAPE_BINARIES:
            assert f'(literal "{exe}")' in text
        # the exec deny sits after the exec allow, so it wins
        assert text.index("(deny process-exec") > text.index("(allow process-exec")


def test_profiles_exclude_launchservices_and_appleevents() -> None:
    for text in (_job(Path("/tmp/x"))[0], _chat()):
        for svc in seatbelt.MACH_DENY_ALWAYS:
            assert svc not in text
        assert "lsopen" not in text and "appleevent-send" not in text


def test_mach_allow_keeps_the_keychain_and_tls_services() -> None:
    for svc in (
        "com.apple.SecurityServer",
        "com.apple.securityd.xpc",
        "com.apple.trustd.agent",
    ):
        assert svc in seatbelt.MACH_ALLOW
        assert svc in _job(Path("/tmp/x"))[0]
    assert not set(seatbelt.MACH_ALLOW) & set(seatbelt.MACH_DENY_ALWAYS)


def test_job_profile_writes_only_inside_its_paths(tmp_path) -> None:
    text, job_dir, cwd = _job(tmp_path)
    assert str(job_dir) in text and str(cwd) in text
    for line in text.splitlines():
        if "allow file-write*" in line:
            assert "subpath" in line or "literal" in line or "regex" in line
    assert '(allow file-write* (subpath "/"))' not in text
    # reads are open everywhere, as `--ro-bind / /`
    assert "(allow file-read*)" in text
    assert f'(deny file-read* (subpath "{HOME}"))' not in text


def test_chat_profile_blocks_home_reads_except_grants() -> None:
    text = _chat()
    deny = f'(deny file-read* (subpath "{HOME}"))'
    assert deny in text
    after = text[text.index(deny) :]
    for rel in _HOME_RO:
        assert f'"{HOME}/{rel}"' in after
    assert '"/Users/tester/tools"' in after  # read grant
    assert '"/Users/tester/ws"' in after  # write grant is readable too
    # a secret that is not granted is not re-allowed
    assert f"{HOME}/.ssh" not in text
    assert "(allow file-read*)" not in text  # chat reads are not blanket


def test_chat_profile_writes_only_the_grants() -> None:
    text = _chat()
    writes = [ln for ln in text.splitlines() if "allow file-write*" in ln]
    assert any("/Users/tester/ws" in ln for ln in writes)
    assert not any(f'"{HOME}"' in ln for ln in writes)
