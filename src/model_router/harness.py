"""Where each harness program is, which version it is, and the Claude
account-separation switches (dv-11a5). One copy, used by every call."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

import structlog

from .types import CLAUDE_HARNESS, CODEX_HARNESS

logger = structlog.get_logger()

# Keeps the server workload apart from the Claude account's apps and devices
# (owner, 2026-10-04). Set on every Claude call the router makes, so it no
# longer depends on what the process happened to inherit from .env.
CLAUDE_ACCOUNT_ENV: Dict[str, str] = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    # Stops the CLI's own side requests. Measured 2026-10-04: through the SDK
    # it otherwise sends the whole prompt a second time, uncached, to the
    # small model to write a session title (`ai-title` in the transcript);
    # +19% on a sonnet extraction. Nothing in the bot reads that title. It is
    # documented to also stop auto-update checks in these processes (not
    # tested here); the owner's own sessions keep updating the shared CLI.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

_VERSION_TIMEOUT_SECONDS = 10.0


def claude_program(configured: Optional[str] = None) -> Optional[str]:
    """Path of the Claude CLI: the configured one, else the one on PATH.

    None only when neither exists; the SDK then falls back to the copy bundled
    with the package, which is older than the installed CLI.
    """
    return configured or shutil.which("claude")


def codex_program(configured: Optional[str] = None) -> Optional[str]:
    return shutil.which(configured or "codex")


class HarnessVersions:
    """``<program> --version``, cached until the program file changes.

    The CLIs update themselves, so the version is read again whenever the
    resolved file's path or mtime differs from the cached one.
    """

    def __init__(
        self, claude_path: Optional[str] = None, codex_bin: Optional[str] = None
    ) -> None:
        self._configured = {CLAUDE_HARNESS: claude_path, CODEX_HARNESS: codex_bin}
        self._cache: Dict[str, Tuple[Tuple[str, float], Optional[str]]] = {}

    def program(self, harness: str) -> Optional[str]:
        configured = self._configured.get(harness)
        if harness == CLAUDE_HARNESS:
            return claude_program(configured)
        if harness == CODEX_HARNESS:
            return codex_program(configured)
        return None

    async def get(self, harness: str) -> Optional[str]:
        program = self.program(harness)
        if not program:
            return None
        try:
            real = os.path.realpath(program)
            key = (real, Path(real).stat().st_mtime)
        except OSError:
            return None
        cached = self._cache.get(harness)
        if cached is not None and cached[0] == key:
            return cached[1]
        version = await _read_version(program)
        self._cache[harness] = (key, version)
        return version


async def _read_version(program: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            program,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("harness version read failed", program=program, error=str(exc))
        return None
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_VERSION_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0][:100] if text else None
