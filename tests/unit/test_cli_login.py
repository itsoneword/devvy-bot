"""Interactive CLI login relay (dv-6a17).

The recorded-output tests are the point. Both CLIs' login output is
undocumented internals that already moved once (Claude dropped OSC-8 wrapping
between 2.1.226 and 2.1.251), so the exact bytes observed on 2026-08-30 are
pinned here — when a CLI update breaks the relay, these fail loudly instead of
the bot silently relaying a mangled link.
"""

import asyncio

from src.bot.features.cli_login import (
    CLAUDE_LOGIN,
    CODEX_LOGIN,
    AuthState,
    InteractiveCliLogin,
    LoginSpec,
    strip_ansi,
)

# Captured 2026-08-30 from `claude auth login --claudeai`, stdout piped,
# CLAUDE_CONFIG_DIR isolated. Note: no escapes at all, URL appears ONCE, and
# the final prompt has NO trailing newline.
CLAUDE_OUTPUT = (
    "Opening browser to sign in…\n"
    "If the browser didn't open, visit: "
    "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    "&code_challenge=xyz&state=123\n"
    "Paste code here if prompted > "
)

# Captured 2026-08-30 from `codex login --device-auth`. SGR colour, no OSC-8;
# URL and code on separate lines; the code is two lines after the URL.
CODEX_OUTPUT = (
    "\nWelcome to Codex [v\x1b[90m0.136.0\x1b[0m]\n"
    "\x1b[90mOpenAI's command-line coding agent\x1b[0m\n\n"
    "Follow these steps to sign in with ChatGPT using device code authorization:\n\n"
    "1. Open this link in your browser and sign in to your account\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
    "   \x1b[94mABCD-EFGHI\x1b[0m\n\n"
    "\x1b[90mDevice codes are a common phishing target. Never share this code.\x1b[0m\n\n"
)

# Emitted first when CODEX_HOME is under /tmp, as a test fixture's would be.
CODEX_TMP_WARNING = (
    "WARNING: proceeding, even though we could not update PATH: Refusing to "
    'create helper binaries under temporary dir "/tmp"\n'
)


def _fake_login(spec: LoginSpec, output: str) -> InteractiveCliLogin:
    """A login whose child has already produced ``output`` and exited 0."""
    login = InteractiveCliLogin(spec)

    class _Stdout:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def read(self, n: int) -> bytes:
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk

    class _Proc:
        # None while running — awaiting_code depends on the child being alive.
        returncode = None
        stdout = None
        stdin = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    proc = _Proc()
    proc.stdout = _Stdout(output.encode())
    login._proc = proc  # type: ignore[assignment]
    return login


# --- escape stripping ---------------------------------------------------


def test_strip_ansi_removes_sgr_colour():
    assert strip_ansi("\x1b[94mhttps://x\x1b[0m") == "https://x"


def test_strip_ansi_removes_osc8_hyperlinks():
    """Claude 2.1.226 wrapped its URL this way. Kept for older CLIs.

    The escape's payload IS the target URL, so stripping the opener drops one
    of the two copies and leaves the visible text — which is why the doubling
    the old ticket warned about resolves itself here.
    """
    wrapped = "visit: \x1b]8;;https://claude.com/a\x07https://claude.com/a\x1b]8;;\x07"
    assert strip_ansi(wrapped) == "visit: https://claude.com/a"


def test_strip_ansi_is_a_noop_on_plain_text():
    """What claude 2.1.251 actually emits."""
    assert strip_ansi(CLAUDE_OUTPUT) == CLAUDE_OUTPUT


# --- recorded output: Claude --------------------------------------------


async def test_claude_url_is_extracted_from_recorded_output():
    login = _fake_login(CLAUDE_LOGIN, CLAUDE_OUTPUT)
    url, code = await login.read_until_prompt()
    assert url == (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
        "&code_challenge=xyz&state=123"
    )
    assert code is None  # Claude prints no code; it reads one


async def test_claude_prompt_without_newline_does_not_hang():
    """The last line has no \\n. A readline() relay would block here forever."""
    login = _fake_login(CLAUDE_LOGIN, CLAUDE_OUTPUT)
    url, _ = await asyncio.wait_for(login.read_until_prompt(), timeout=5)
    assert url is not None


async def test_claude_still_works_if_osc8_wrapping_comes_back():
    """Format regression insurance: 2.1.226's shape must still parse."""
    old = (
        "If the browser didn't open, visit: "
        "\x1b]8;;https://claude.com/cai/oauth/authorize?state=1\x07"
        "https://claude.com/cai/oauth/authorize?state=1\x1b]8;;\x07\n"
        "Paste code here if prompted > "
    )
    login = _fake_login(CLAUDE_LOGIN, old)
    url, _ = await login.read_until_prompt()
    assert url is not None and url.startswith("https://claude.com/cai/oauth/authorize")


# --- recorded output: Codex ---------------------------------------------


async def test_codex_extracts_both_url_and_code():
    """The old relay sent the URL and never the code. This is that bug."""
    login = _fake_login(CODEX_LOGIN, CODEX_OUTPUT)
    url, code = await login.read_until_prompt()
    assert url == "https://auth.openai.com/codex/device"
    assert code == "ABCD-EFGHI"


async def test_codex_code_is_not_the_label_line():
    """`"code" in text.lower()` matches '2. Enter this one-time code' first."""
    login = _fake_login(CODEX_LOGIN, CODEX_OUTPUT)
    _, code = await login.read_until_prompt()
    assert "Enter" not in (code or "")


async def test_codex_tolerates_the_tmp_path_warning():
    login = _fake_login(CODEX_LOGIN, CODEX_TMP_WARNING + CODEX_OUTPUT)
    url, code = await login.read_until_prompt()
    assert url == "https://auth.openai.com/codex/device"
    assert code == "ABCD-EFGHI"


# --- direction of the two flows -----------------------------------------


def test_claude_needs_a_code_back_and_codex_does_not():
    """The mirror-image property the whole design rests on."""
    assert CLAUDE_LOGIN.needs_code_from_user is True
    assert CODEX_LOGIN.needs_code_from_user is False
    assert CLAUDE_LOGIN.code_pattern is None
    assert CODEX_LOGIN.code_pattern is not None


async def test_awaiting_code_is_false_for_codex():
    """Nothing may consume a user's next message on the Codex path."""
    login = _fake_login(CODEX_LOGIN, CODEX_OUTPUT)
    await login.read_until_prompt()
    assert login.awaiting_code is False


async def test_awaiting_code_is_true_for_claude_until_submitted():
    login = _fake_login(CLAUDE_LOGIN, CLAUDE_OUTPUT)
    await login.read_until_prompt()
    assert login.awaiting_code is True
    login._code_sent = True
    assert login.awaiting_code is False


# --- failure handling ---------------------------------------------------


async def test_no_url_within_the_deadline_returns_none_not_a_hang():
    spec = LoginSpec(
        name="fake",
        argv=["true"],
        url_pattern=CLAUDE_LOGIN.url_pattern,
        url_timeout=0.3,
    )
    login = _fake_login(spec, "some unrelated output\n")
    url, _ = await asyncio.wait_for(login.read_until_prompt(), timeout=5)
    assert url is None


async def test_real_process_that_prints_nothing_is_killed_by_timeout():
    """An abandoned login must not sit on a pipe forever."""
    spec = LoginSpec(
        name="sleeper",
        argv=["sleep", "30"],
        url_pattern=CLAUDE_LOGIN.url_pattern,
        url_timeout=0.3,
        timeout=0.3,
    )
    login = InteractiveCliLogin(spec)
    await login.start()
    url, _ = await login.read_until_prompt()
    assert url is None
    result = await login.wait()
    assert result.ok is False
    assert "timed out" in result.detail
    await login.kill()


# --- status rendering ---------------------------------------------------


def test_unverified_positive_is_labelled_not_presented_as_fact():
    """codex login status lied for three months; never render it as truth."""
    state = AuthState("Codex", True, "Logged in using ChatGPT", verified=False)
    rendered = state.render()
    assert "⚠️" in rendered
    assert "not verified" in rendered


def test_verified_positive_is_clean():
    state = AuthState(
        "Claude", True, "logged in (claude.ai · plan: max)", verified=True
    )
    rendered = state.render()
    assert "✅" in rendered
    assert "not verified" not in rendered


def test_unknown_state_is_distinct_from_logged_out():
    assert "❔" in AuthState("Codex", None, "CLI not installed", verified=True).render()
    assert "❌" in AuthState("Codex", False, "not logged in", verified=True).render()
