"""Telegram approve/deny gate for destructive ops on full-access profiles.

Bridges the SDK ``can_use_tool`` callback (which runs inside the agent's
execution and must return Allow/Deny) to an interactive Telegram prompt. When a
full-access (unsandboxed) agent attempts a destructive op, the run pauses on an
asyncio Future while the owner is asked to approve or deny.

Prompts go to the configured owner chat(s) (``NOTIFICATION_CHAT_IDS``) — the
threat model is an *unattended* run, so the decision belongs to the owner, not
whatever chat (if any) triggered the run.

Fails **closed**: on timeout or with no owner chat configured, the op is denied.

This is the one place every ask card is built -- chat (Claude hook), Codex
(bridge ``/policy/check``) and background jobs (``/internal/policy/ask``) all
end in :meth:`ConfirmationManager.request`. The independent explanation
(dv-af70, ``src/security/ask_explain.py``) is added here too: the card goes out
at once, and the explanation is edited in under the code when it arrives.
"""

from __future__ import annotations

import asyncio
import html
import time
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

if TYPE_CHECKING:
    from ..security.ask_explain import AskContext

from .turn_activity import note_owner_wait

logger = structlog.get_logger()

# Telegram callback_data is capped at 64 bytes: "confirm:<hex32>:y" fits.
CALLBACK_PREFIX = "confirm"


class ConfirmationManager:
    """Owner approve/deny gate backed by asyncio Futures + Telegram buttons."""

    def __init__(
        self,
        send_prompt: Callable[[int, str, str], Awaitable[int]],
        chat_ids: Optional[List[int]],
        timeout_seconds: float = 600.0,
        edit_prompt: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
        explain: Optional[
            Callable[[str, str, Optional["AskContext"]], Awaitable[Optional[str]]]
        ] = None,
        update_prompt: Optional[Callable[[int, int, str, str], Awaitable[None]]] = None,
    ) -> None:
        # send_prompt(chat_id, text, token) -> sends a message with two inline
        # buttons whose callback_data is f"{CALLBACK_PREFIX}:{token}:y|n", and
        # returns the sent message's id (so a timeout can edit it in place).
        self._send_prompt = send_prompt
        # edit_prompt(chat_id, message_id, text) -> rewrites the prompt once
        # it's resolved by timeout, so stale Approve/Deny buttons don't sit
        # there looking actionable after the decision has already been made.
        self._edit_prompt = edit_prompt
        # explain(reason, preview, context) -> an HTML block for under the
        # code, or None (dv-af70). It sees the reason, the command and the
        # ask's task context (profile, job, task id + title, stage; dv-781f),
        # never the agent's reasoning. update_prompt(chat_id, message_id, text, token) edits the
        # card in place and rebuilds the Approve/Deny buttons for the token
        # (an edit without reply_markup would drop them).
        self._explain = explain
        self._update_prompt = update_prompt
        self._chat_ids = list(chat_ids or [])
        self._timeout = timeout_seconds
        self._pending: Dict[str, asyncio.Future[bool]] = {}
        self._pending_messages: Dict[str, Tuple[int, int]] = {}
        # The card's current text, so a timeout rewrite keeps an explanation.
        self._pending_texts: Dict[str, str] = {}

    async def request(
        self,
        reason: str,
        preview: str,
        *,
        timeout_seconds: Optional[float] = None,
        source: Optional[str] = None,
        context: Optional["AskContext"] = None,
    ) -> bool:
        """Ask the owner to approve a destructive op. Returns True if approved.

        ``timeout_seconds`` overrides the manager's default: an unattended
        background job waits an hour (owner, dv-ef90), a chat turn two
        minutes. ``source`` names who is asking (a job's name) in the prompt.
        ``context`` is what the explainer may know about the ask (dv-781f).
        """
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
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

        head = (
            "⚠️ <b>Confirm risky action</b>\n"
            + (f"from background job <i>{html.escape(source)}</i>\n" if source else "")
            + f"<b>{html.escape(reason)}</b>\n\n"
            f"<pre>{html.escape(_truncate(preview, 500))}</pre>\n"
        )
        tail = (
            "Approve to run it, or deny to block. "
            f"Denied automatically in {_human_duration(timeout)}."
        )
        text = head + tail
        chat_id = self._chat_ids[0]
        try:
            message_id = await self._send_prompt(chat_id, text, token)
        except Exception as exc:
            logger.warning("Failed to send confirmation prompt", error=str(exc))
            self._pending.pop(token, None)
            return False
        explaining: Optional[asyncio.Task[None]] = None
        if message_id is not None:
            self._pending_messages[token] = (chat_id, message_id)
            self._pending_texts[token] = text
            if self._explain and self._update_prompt:
                explaining = asyncio.create_task(
                    self._add_explanation(token, reason, preview, context, head, tail)
                )

        # The owner's think time, measured from the card going out. It is
        # charged to the calling hook so the turn ledger does not report it
        # as a slow hook (dv-d760).
        asked_at = time.monotonic()
        log_fields = {"reason": reason, "source": source, "preview": preview[:120]}
        try:
            approved = await asyncio.wait_for(fut, timeout=timeout)
            waited = time.monotonic() - asked_at
            note_owner_wait(waited)
            logger.info(
                "Confirmation resolved",
                approved=approved,
                waited_s=round(waited, 2),
                **log_fields,
            )
            return approved
        except asyncio.TimeoutError:
            waited = time.monotonic() - asked_at
            note_owner_wait(waited)
            logger.info(
                "Confirmation timed out -> denied",
                waited_s=round(waited, 2),
                **log_fields,
            )
            await self._mark_timed_out(token, self._pending_texts.get(token, text))
            return False
        finally:
            if explaining is not None:
                explaining.cancel()
            self._pending.pop(token, None)
            self._pending_messages.pop(token, None)
            self._pending_texts.pop(token, None)

    async def _add_explanation(
        self,
        token: str,
        reason: str,
        preview: str,
        context: Optional["AskContext"],
        head: str,
        tail: str,
    ) -> None:
        """Edit the explanation in under the code once it arrives (dv-af70).

        Advisory only: it never resolves the ask. No answer, a failure or an
        ask already decided leaves the card as it is.
        """
        assert self._explain is not None and self._update_prompt is not None
        try:
            block = await self._explain(reason, preview, context)
        except Exception as exc:
            logger.info("Ask explanation failed", reason=reason, error=str(exc))
            return
        fut = self._pending.get(token)
        location = self._pending_messages.get(token)
        if not block or fut is None or fut.done() or location is None:
            return
        text = f"{head}{block}\n\n{tail}"
        self._pending_texts[token] = text
        try:
            await self._update_prompt(location[0], location[1], text, token)
        except Exception as exc:
            logger.info("Failed to add ask explanation to card", error=str(exc))

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


def _human_duration(seconds: float) -> str:
    total = int(seconds)
    if total >= 3600 and total % 3600 == 0:
        hours = total // 3600
        return f"{hours}h"
    if total >= 60 and total % 60 == 0:
        return f"{total // 60}min"
    return f"{total}s"
