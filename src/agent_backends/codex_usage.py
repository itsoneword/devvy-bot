"""Read Codex account limits through the local App Server.

This is deliberately separate from ``codex exec``: account/rateLimits/read and
account/usage/read are metadata calls and do not start an LLM turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger()


class CodexUsageError(RuntimeError):
    """The local Codex App Server could not return account data."""


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate the app-server process group and reap it."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await proc.wait()
        except ProcessLookupError:
            pass


class CodexUsageClient:
    """Small one-shot JSONL client for Codex's local App Server."""

    def __init__(self, codex_bin: str = "codex", timeout: float = 10.0) -> None:
        self.codex_bin = codex_bin
        self.timeout = timeout

    async def read(self) -> Dict[str, Any]:
        """Return rate limits and usage, without exposing raw auth data."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise CodexUsageError("Codex App Server недоступен") from exc

        try:
            await self._send(
                proc,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "devvybot",
                            "version": "1.0",
                        },
                        "capabilities": {"experimentalApi": False},
                    },
                },
            )
            await self._response(proc, 1)
            await self._send(proc, {"method": "initialized"})

            await self._send(
                proc,
                {"method": "account/rateLimits/read", "id": 2, "params": None},
            )
            rate_limits = await self._response(proc, 2)

            await self._send(
                proc,
                {"method": "account/usage/read", "id": 3, "params": None},
            )
            try:
                usage = await self._response(proc, 3)
            except CodexUsageError as exc:
                # Rate limits are the important part. Older/limited accounts
                # may not expose the historical usage endpoint.
                logger.info("Codex account usage unavailable", reason=str(exc))
                usage = {}

            return {"rate_limits": rate_limits, "usage": usage}
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, RuntimeError):
                    pass
            await _stop_process(proc)

    async def _send(
        self, proc: asyncio.subprocess.Process, message: Dict[str, Any]
    ) -> None:
        if proc.stdin is None:
            raise CodexUsageError("Codex App Server stdin недоступен")
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            raise CodexUsageError("Codex App Server закрыл соединение") from exc

    async def _response(
        self, proc: asyncio.subprocess.Process, request_id: int
    ) -> Dict[str, Any]:
        if proc.stdout is None:
            raise CodexUsageError("Codex App Server stdout недоступен")
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), self.timeout)
            except asyncio.TimeoutError as exc:
                raise CodexUsageError("Codex App Server не ответил вовремя") from exc
            if not raw:
                raise CodexUsageError("Codex App Server завершился без ответа")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                raise CodexUsageError(str(error.get("message") or "запрос отклонён"))
            result = message.get("result")
            return result if isinstance(result, dict) else {}


def _window_label(minutes: Optional[int]) -> str:
    if not minutes:
        return "неизвестное окно"
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days} дн."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes} мин"


def _reset_label(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "неизвестно"


def _format_tokens(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "неизвестно"


def format_codex_limits(snapshot: Dict[str, Any]) -> str:
    """Format only stable, non-sensitive account fields for Telegram."""
    payload = snapshot.get("rate_limits") or {}
    limits = payload.get("rateLimitsByLimitId") or {}
    if not limits:
        one = payload.get("rateLimits")
        if isinstance(one, dict):
            limits = {one.get("limitId") or "codex": one}

    lines = ["📊 Codex limits"]
    plan = next(
        (
            item.get("planType")
            for item in limits.values()
            if isinstance(item, dict) and item.get("planType")
        ),
        None,
    )
    if plan:
        lines.append(f"Plan: {plan}")

    displayed_window = False
    for name, item in limits.items():
        if not isinstance(item, dict):
            continue
        primary = item.get("primary")
        secondary = item.get("secondary")
        for label, window in (("Основное", primary), ("Вторичное", secondary)):
            if not isinstance(window, dict):
                continue
            displayed_window = True
            used = window.get("usedPercent")
            used_text = f"{used}%" if isinstance(used, (int, float)) else "?"
            duration = _window_label(window.get("windowDurationMins"))
            reset = _reset_label(window.get("resetsAt"))
            prefix = f"{name} · " if len(limits) > 1 else ""
            lines.append(f"{prefix}{label}: {duration} · использовано {used_text}")
            lines.append(f"Сброс: {reset}")

        credits = item.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited"):
                lines.append("Credits: unlimited")
            elif credits.get("hasCredits"):
                lines.append(f"Credits: {credits.get('balance') or 'есть'}")
            else:
                lines.append("Credits: нет")

    if not displayed_window:
        lines.append("Активное окно лимита не сообщено аккаунтом.")

    usage = snapshot.get("usage") or {}
    summary = usage.get("summary") or {}
    if summary:
        lifetime = summary.get("lifetimeTokens")
        peak = summary.get("peakDailyTokens")
        if lifetime is not None:
            lines.append(f"Токены за всю историю аккаунта: {_format_tokens(lifetime)}")
        if peak is not None:
            lines.append(f"Пиковые токены за день (аккаунт): {_format_tokens(peak)}")

    if not limits and not summary:
        lines.append("Данные аккаунта недоступны.")
    return "\n".join(lines)
