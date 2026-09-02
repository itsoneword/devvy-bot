"""Telegram approve/deny gate for destructive ops on full-access profiles.

Bridges the SDK ``can_use_tool`` callback (which runs inside the agent's
execution and must return Allow/Deny) to an interactive Telegram prompt. When a
full-access (unsandboxed) agent attempts a destructive op, the run pauses on an
asyncio Future while the owner is asked to approve or deny.

Prompts go to the configured owner chat(s) (``NOTIFICATION_CHAT_IDS``) — the
threat model is an *unattended* run, so the decision belongs to the owner, not
whatever chat (if any) triggered the run.

Fails **closed**: on timeout or with no owner chat configured, the op is denied.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Telegram callback_data is capped at 64 bytes: "confirm:<hex32>:y" fits.
CALLBACK_PREFIX = "confirm"


class ConfirmationManager:
    """Owner approve/deny gate backed by asyncio Futures + Telegram buttons."""

    def __init__(
        self,
        send_prompt: Callable[[int, str, str], Awaitable[int]],
        chat_ids: Optional[List[int]],
        timeout_seconds: float = 120.0,
        edit_prompt: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    ) -> None:
        # send_prompt(chat_id, text, token) -> sends a message with two inline
        # buttons whose callback_data is f"{CALLBACK_PREFIX}:{token}:y|n", and
        # returns the sent message's id (so a timeout can edit it in place).
        self._send_prompt = send_prompt
        # edit_prompt(chat_id, message_id, text) -> rewrites the prompt once
        # it's resolved by timeout, so stale Approve/Deny buttons don't sit
        # there looking actionable after the decision has already been made.
        self._edit_prompt = edit_prompt
        self._chat_ids = list(chat_ids or [])
        self._timeout = timeout_seconds
        self._pending: Dict[str, asyncio.Future[bool]] = {}
        self._pending_messages: Dict[str, Tuple[int, int]] = {}

    async def request(self, reason: str, preview: str) -> bool:
        """Ask the owner to approve a destructive op. Returns True if approved."""
        if not self._chat_ids:
            logger.warning(
                "Destructive op denied: no owner chat configured for confirmation",
                reason=reason,
            )
            return False

        token = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending[token] = fut

        text = (
            "⚠️ <b>Confirm risky action</b>\n"
            f"<b>{reason}</b>\n\n"
            f"<pre>{_truncate(preview, 500)}</pre>\n"
            "Approve to run it, or deny to block. "
            f"Denied automatically in {int(self._timeout)}s."
        )
        chat_id = self._chat_ids[0]
        try:
            message_id = await self._send_prompt(chat_id, text, token)
        except Exception as exc:
            logger.warning("Failed to send confirmation prompt", error=str(exc))
            self._pending.pop(token, None)
            return False
        if message_id is not None:
            self._pending_messages[token] = (chat_id, message_id)

        try:
            approved = await asyncio.wait_for(fut, timeout=self._timeout)
            logger.info("Confirmation resolved", reason=reason, approved=approved)
            return approved
        except asyncio.TimeoutError:
            logger.info("Confirmation timed out -> denied", reason=reason)
            await self._mark_timed_out(token, text)
            return False
        finally:
            self._pending.pop(token, None)
            self._pending_messages.pop(token, None)

    async def _mark_timed_out(self, token: str, original_text: str) -> None:
        """Rewrite the prompt in place so it no longer looks actionable."""
        if not self._edit_prompt:
            return
        location = self._pending_messages.get(token)
        if not location:
            return
        chat_id, message_id = location
        try:
            await self._edit_prompt(
                chat_id,
                message_id,
                f"{original_text}\n\n⛔ <b>Denied (timed out — no response received)</b>",
            )
        except Exception as exc:
            logger.debug(
                "Failed to edit confirmation message on timeout", error=str(exc)
            )

    def resolve(self, token: str, approved: bool) -> bool:
        """Resolve a pending confirmation (called by the callback handler).

        Returns True if a pending request matched this token; False if it was
        already resolved (timed out or double-tapped) — the caller should not
        treat a False return as if the tap had any effect.
        """
        fut = self._pending.get(token)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
