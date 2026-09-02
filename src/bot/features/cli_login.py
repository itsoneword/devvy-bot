"""Drive a CLI's interactive login from Telegram (dv-6a17).

Both supported backends authenticate through a browser, and neither can do it
from a Telegram message on its own. This module spawns the CLI with pipes,
pulls the link (and, for Codex, the one-time code) out of its output, hands
them to the caller to relay, and — for Claude only — writes the user's reply
back to the child's stdin.

**The two flows are mirror images.** See docs/FINDINGS.md 2026-08-30:

    Claude:  prints ONE url          -> user pastes a code INTO stdin
    Codex:   prints a url AND a code -> user types the code in the browser,
                                        nothing is ever sent back

So ``needs_code_from_user`` is not an accident of the implementation; it is the
actual difference between the two, and the Codex path having ``stdin=DEVNULL``
is correct rather than a missing feature.

Two things learned the hard way and encoded here:

* **Read bytes, not lines.** Claude's final prompt (``Paste code here if
  prompted > ``) has no trailing newline, so anything built on ``readline()``
  blocks on it forever and never learns the child is waiting.
* **Never anchor on escapes or line numbers.** Claude v2.1.226 wrapped its URL
  in OSC-8 and printed it twice; v2.1.251 emits no escapes at all and prints it
  once. Strip whatever is there and match with a regex.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import List, Optional

import structlog

logger = structlog.get_logger()

# CSI/SGR ("\x1b[94m") and OSC-8 hyperlink wrappers ("\x1b]8;;URL\x07TEXT").
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    """Remove OSC and CSI sequences, leaving the visible text.

    A no-op on output that has none -- which is what Claude emits today. Kept
    because the format has already changed once between patch releases.
    """
    return _CSI_RE.sub("", _OSC_RE.sub("", text))


@dataclass(frozen=True)
class LoginSpec:
    """How to log one CLI in."""

    name: str  # "claude" / "codex", used in messages and logs
    argv: List[str]
    url_pattern: re.Pattern[str]
    # Codex prints a one-time code the user types into the browser. Claude does
    # not -- it *reads* one. At most one of these is set per spec.
    code_pattern: Optional[re.Pattern[str]] = None
    needs_code_from_user: bool = False
    # Codex states 15 minutes; Claude states nothing. Both are killed rather
    # than left holding a pipe forever.
    timeout: float = 900.0
    # How long to wait for the URL itself before giving up loudly. Failing here
    # means the CLI's output format moved -- that must be visible, not silent.
    url_timeout: float = 45.0
    env: dict = field(default_factory=dict)


CLAUDE_LOGIN = LoginSpec(
    name="claude",
    argv=["claude", "auth", "login", "--claudeai"],
    url_pattern=re.compile(r"https://claude\.com/\S+"),
    needs_code_from_user=True,
)

CODEX_LOGIN = LoginSpec(
    name="codex",
    argv=["codex", "login", "--device-auth"],
    url_pattern=re.compile(r"https://\S*openai\.com/\S*device\S*"),
    # The code sits on its own line after "2. Enter this one-time code".
    # Matching the label instead of the code is the bug in the old relay.
    code_pattern=re.compile(r"^\s*([A-Z0-9]{4,}-[A-Z0-9]{4,})\s*$", re.M),
    needs_code_from_user=False,
    timeout=900.0,
)


@dataclass
class LoginResult:
    ok: bool
    detail: str


class InteractiveCliLogin:
    """One in-flight login. Not reusable -- construct one per attempt."""

    def __init__(self, spec: LoginSpec) -> None:
        self.spec = spec
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._buf = ""
        self._code_sent = False

    @property
    def awaiting_code(self) -> bool:
        """True while the child is blocked on stdin waiting for a code."""
        return (
            self.spec.needs_code_from_user
            and not self._code_sent
            and self._proc is not None
            and self._proc.returncode is None
        )

    async def start(self) -> None:
        env = None
        if self.spec.env:
            import os

            env = {**os.environ, **self.spec.env}
        self._proc = await asyncio.create_subprocess_exec(
            *self.spec.argv,
            stdin=(
                asyncio.subprocess.PIPE
                if self.spec.needs_code_from_user
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

    async def read_until_prompt(self) -> tuple[Optional[str], Optional[str]]:
        """Accumulate output until the URL (and code, if any) are known.

        Returns ``(url, code)``. Reads in chunks rather than lines: the Claude
        prompt that signals "ready for input" carries no newline.
        """
        assert self._proc is not None and self._proc.stdout is not None
        deadline = asyncio.get_running_loop().time() + self.spec.url_timeout
        url: Optional[str] = None
        code: Optional[str] = None
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                chunk = await asyncio.wait_for(
                    self._proc.stdout.read(1024), timeout=max(remaining, 0.1)
                )
            except asyncio.TimeoutError:
                break
            if not chunk:
                break  # child exited before printing a link
            self._buf += strip_ansi(chunk.decode(errors="replace"))
            if url is None:
                found = self.spec.url_pattern.search(self._buf)
                url = found.group(0).rstrip(".,)") if found else None
            if code is None and self.spec.code_pattern is not None:
                found = self.spec.code_pattern.search(self._buf)
                code = found.group(1) if found else None
            done = url is not None and (
                self.spec.code_pattern is None or code is not None
            )
            if done:
                return url, code
        return url, code

    async def submit_code(self, code: str) -> None:
        """Write the user's code to the child's stdin (Claude only)."""
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(code.strip().encode() + b"\n")
        await self._proc.stdin.drain()
        self._code_sent = True

    async def wait(self) -> LoginResult:
        """Wait for the child to finish; kill and report on timeout.

        The tail of the output is included on failure so a format change or an
        expired code is diagnosable, but only the tail: the buffer can contain
        the code the user just typed.
        """
        assert self._proc is not None
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            await self.kill()
            return LoginResult(
                False, f"{self.spec.name} login timed out and was cancelled."
            )
        if self._proc.returncode == 0:
            return LoginResult(True, f"{self.spec.name} login complete.")
        tail = "\n".join(self._buf.strip().splitlines()[-3:])
        return LoginResult(
            False,
            f"{self.spec.name} login failed (exit {self._proc.returncode}).\n{tail}",
        )

    async def kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            await self._proc.wait()


# --- status probes ------------------------------------------------------


@dataclass(frozen=True)
class AuthState:
    """What we know about one backend's login, and how sure we are.

    ``verified`` is the load-bearing field. ``claude auth status --json`` asks
    the server and reports truth. ``codex login status`` reads the local
    credential FILE and prints "Logged in using ChatGPT" against a session the
    server rejects -- it did exactly that here for three months
    (docs/FINDINGS.md 2026-08-30). Reporting both the same way would put a
    confident lie in front of the user, so an unverified claim is labelled.
    """

    backend: str
    logged_in: Optional[bool]  # None = could not tell
    detail: str
    verified: bool

    def render(self) -> str:
        if self.logged_in is None:
            icon = "❔"
        elif self.logged_in and self.verified:
            icon = "✅"
        elif self.logged_in:
            icon = "⚠️"
        else:
            icon = "❌"
        line = f"{icon} <b>{self.backend}</b> — {self.detail}"
        if self.logged_in and not self.verified:
            line += "\n    <i>reported by the CLI, not verified against the server</i>"
        return line


async def _run(argv: List[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, FileNotFoundError):
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, strip_ansi(out.decode(errors="replace"))


async def probe_claude() -> AuthState:
    """Real state: `claude auth status --json` asks the server."""
    import json

    rc, out = await _run(["claude", "auth", "status", "--json"])
    if rc == 127:
        return AuthState("Claude", None, "CLI not installed", verified=True)
    try:
        data = json.loads(out[out.index("{") : out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return AuthState("Claude", None, "could not read auth status", verified=False)
    if not data.get("loggedIn"):
        return AuthState("Claude", False, "not logged in", verified=True)
    bits = [str(data.get("authMethod") or "")]
    if data.get("subscriptionType"):
        bits.append(f"plan: {data['subscriptionType']}")
    if data.get("email"):
        bits.append(str(data["email"]))
    detail = "logged in (" + " · ".join(b for b in bits if b) + ")"
    return AuthState("Claude", True, detail, verified=True)


async def probe_codex() -> AuthState:
    """Best effort: `codex login status` reads the file, not the server.

    Never returns verified=True for a positive result. See AuthState.
    """
    rc, out = await _run(["codex", "login", "status"])
    if rc == 127:
        return AuthState("Codex", None, "CLI not installed", verified=True)
    text = " ".join(out.split())
    if rc != 0 or "not logged in" in text.lower():
        return AuthState("Codex", False, "not logged in", verified=True)
    return AuthState("Codex", True, text or "logged in", verified=False)


PROBES: dict = {"claude": probe_claude, "codex": probe_codex}
SPECS: dict = {"claude": CLAUDE_LOGIN, "codex": CODEX_LOGIN}


async def probe_all() -> List[AuthState]:
    return [await PROBES[name]() for name in ("claude", "codex")]
